"""SQLite persistence for the desk. One connection per call."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from config import SETTINGS_DEFAULTS, load_seed

AGENT_NAMES = ("scanner", "auditor", "analyst", "executor", "risk")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    pool TEXT,
    migrated_at REAL,
    creator TEXT,
    creator_balance_at_migration REAL,
    holders INTEGER
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    ts REAL,
    liquidity_usd REAL,
    top10_pct REAL,
    h1_change_pct REAL,
    age_min REAL,
    verdict TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    ts REAL,
    layer TEXT,
    name TEXT,
    value_text TEXT,
    "pass" INTEGER CHECK ("pass" IN (0, 1)),
    source TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    ts REAL,
    alive_6h INTEGER CHECK (alive_6h IN (0, 1)),
    organic INTEGER CHECK (organic IN (0, 1)),
    flush INTEGER CHECK (flush IN (0, 1)),
    chase INTEGER CHECK (chase IN (0, 1)),
    score REAL,
    "pass" INTEGER CHECK ("pass" IN (0, 1)),
    reason TEXT,
    input_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    ts REAL,
    price REAL,
    size_usd REAL,
    stack_pct REAL,
    link TEXT,
    mode TEXT CHECK (mode IN ('PAPER', 'LIVE')),
    status TEXT,
    push_title TEXT,
    push_body TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    opened_at REAL,
    entry_price REAL,
    size_usd REAL,
    peak_price REAL,
    stop_price REAL,
    trail_active INTEGER CHECK (trail_active IN (0, 1)),
    closed_at REAL,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    fees_usd REAL,
    mode TEXT CHECK (mode IN ('PAPER', 'LIVE')),
    trail_giveback_pct REAL,
    entry_fee_usd REAL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    kind TEXT CHECK (kind IN ('bought', 'sold', 'stopped', 'rejected')),
    mint TEXT,
    agent TEXT,
    title TEXT,
    subtitle TEXT,
    value TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    mint TEXT,
    ts REAL,
    price REAL,
    volume_m5 REAL
);

CREATE TABLE IF NOT EXISTS balance (
    ts REAL,
    value REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS change_log (
    ts REAL,
    key TEXT,
    "old" TEXT,
    "new" TEXT,
    who TEXT
);

CREATE TABLE IF NOT EXISTS skips (
    mint TEXT,
    "until" REAL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    agent TEXT PRIMARY KEY,
    ts REAL
);

CREATE TABLE IF NOT EXISTS push_subs (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT,
    auth TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS ws_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    kind TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    window_id TEXT PRIMARY KEY,
    url TEXT,
    acked_at REAL,
    skips_cleared INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ws_events_ts ON ws_events (ts);

CREATE TABLE IF NOT EXISTS push_outbox (
    event_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (event_id, endpoint)
);

CREATE TABLE IF NOT EXISTS restart_events (
    event_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    down_min INTEGER,
    push_text TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    label TEXT NOT NULL,
    provider TEXT NOT NULL,
    provenance TEXT NOT NULL,
    wallet TEXT,
    amount TEXT NOT NULL,
    amount_unit TEXT NOT NULL,
    sell_chain TEXT NOT NULL,
    buy_chain TEXT NOT NULL,
    sell_native INTEGER NOT NULL,
    buy_native INTEGER NOT NULL,
    sell_mint TEXT,
    buy_mint TEXT,
    sell_decimals INTEGER,
    buy_decimals INTEGER,
    exact_out INTEGER NOT NULL,
    slippage REAL NOT NULL,
    expiry REAL,
    out_amount TEXT,
    provider_quote_id TEXT,
    failure TEXT,
    request_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    observed_at REAL,
    import_json TEXT
);

CREATE TABLE IF NOT EXISTS phantom_control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS phantom_reservations (
    intent_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    wallet_id TEXT,
    wallet TEXT,
    chain TEXT,
    pair_json TEXT NOT NULL,
    amount_usd TEXT,
    fee_usd TEXT,
    reserved_usd TEXT,
    slippage REAL,
    entry_quote_id TEXT,
    exit_quote_id TEXT,
    fx_sol_usd TEXT,
    fx_at REAL,
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    realized_pnl TEXT,
    entry_price TEXT,
    exit_price TEXT,
    mark_price TEXT
);

CREATE TABLE IF NOT EXISTS phantom_alerts (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prices_mint_ts ON prices (mint, ts);
CREATE INDEX IF NOT EXISTS idx_scans_mint_ts ON scans (mint, ts);
CREATE INDEX IF NOT EXISTS idx_audits_mint_ts ON audits (mint, ts);
CREATE INDEX IF NOT EXISTS idx_verdicts_mint_ts ON verdicts (mint, ts);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity (ts);
CREATE INDEX IF NOT EXISTS idx_positions_closed_at ON positions (closed_at);
CREATE INDEX IF NOT EXISTS idx_balance_ts ON balance (ts);
CREATE INDEX IF NOT EXISTS idx_heartbeats_agent ON heartbeats (agent);
"""


def _data_dir() -> Path:
    return Path(os.environ.get("DESK_DATA_DIR") or "./data")


def _db_file() -> Path:
    return _data_dir() / "desk.db"


def _setting_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def connect() -> sqlite3.Connection:
    """Open ${DESK_DATA_DIR}/desk.db. Caller closes it. Do not log the path."""
    directory = _data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(directory / "desk.db")
    except OSError:
        raise RuntimeError("DESK_DATA_DIR is not usable") from None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Create the schema if missing and seed settings without overwriting."""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        now = time.time()
        for key, value in SETTINGS_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, _setting_text(value), now),
            )
        for key, value in load_seed().items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, _setting_text(value), now),
            )
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            # Stand-in from 6de36bd. Columns are unchanged; this only corrects
            # that default when the owner has not already replaced it.
            conn.execute(
                "UPDATE settings SET value = '12', updated_at = ? WHERE key = 'trail_pct' AND value = '13'",
                (now,),
            )
            conn.execute("PRAGMA user_version=2")
        if version < 3:
            # Additive columns only. Existing rows, subscriptions, and owner
            # settings stay. notifier moves telegram -> webpush only when the
            # owner never wrote a change_log row for it.
            for table, column, ddl in (
                ("signals", "push_title", "ALTER TABLE signals ADD COLUMN push_title TEXT"),
                ("signals", "push_body", "ALTER TABLE signals ADD COLUMN push_body TEXT"),
                ("positions", "trail_giveback_pct", "ALTER TABLE positions ADD COLUMN trail_giveback_pct REAL"),
            ):
                have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    conn.execute(ddl)
            owned = conn.execute(
                "SELECT 1 FROM change_log WHERE key = 'notifier' LIMIT 1"
            ).fetchone()
            if owned is None:
                conn.execute(
                    """
                    UPDATE settings SET value = 'webpush', updated_at = ?
                    WHERE key = 'notifier' AND value = 'telegram'
                    """,
                    (now,),
                )
            conn.execute("PRAGMA user_version=3")
        if version < 4:
            have = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            if "entry_fee_usd" not in have:
                conn.execute("ALTER TABLE positions ADD COLUMN entry_fee_usd REAL")
            conn.execute("PRAGMA user_version=4")
        if version < 5:
            conn.execute("PRAGMA user_version=5")
        if version < 6:
            conn.execute("PRAGMA user_version=6")
        if version < 7:
            conn.execute("PRAGMA user_version=7")
        if version < 8:
            # Version 7 stored a manual note. That table was never deployed.
            # Replace it with the immutable pair contract.
            conn.execute("DROP TABLE IF EXISTS quotes")
            conn.executescript("""
                CREATE TABLE quotes (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    label TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    wallet TEXT,
                    amount TEXT NOT NULL,
                    amount_unit TEXT NOT NULL,
                    sell_chain TEXT NOT NULL,
                    buy_chain TEXT NOT NULL,
                    sell_native INTEGER NOT NULL,
                    buy_native INTEGER NOT NULL,
                    sell_mint TEXT,
                    buy_mint TEXT,
                    sell_decimals INTEGER,
                    buy_decimals INTEGER,
                    exact_out INTEGER NOT NULL,
                    slippage REAL NOT NULL,
                    expiry REAL,
                    out_amount TEXT,
                    provider_quote_id TEXT,
                    failure TEXT,
                    request_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    observed_at REAL,
                    import_json TEXT
                );
            """)
            conn.execute("PRAGMA user_version=8")
        if version < 9:
            have = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
            if "observed_at" not in have:
                conn.execute("ALTER TABLE quotes ADD COLUMN observed_at REAL")
            if "import_json" not in have:
                conn.execute("ALTER TABLE quotes ADD COLUMN import_json TEXT")
            conn.execute("PRAGMA user_version=9")
        if version < 10:
            conn.execute(
                "INSERT OR IGNORE INTO phantom_control (key, value, updated_at) VALUES ('halt', '1', ?)",
                (now,),
            )
            conn.execute("PRAGMA user_version=10")
        if version < 11:
            have = {row[1] for row in conn.execute("PRAGMA table_info(phantom_reservations)")}
            for column, ddl in (
                ("realized_pnl", "ALTER TABLE phantom_reservations ADD COLUMN realized_pnl TEXT"),
                ("entry_price", "ALTER TABLE phantom_reservations ADD COLUMN entry_price TEXT"),
                ("exit_price", "ALTER TABLE phantom_reservations ADD COLUMN exit_price TEXT"),
                ("mark_price", "ALTER TABLE phantom_reservations ADD COLUMN mark_price TEXT"),
            ):
                if column not in have:
                    conn.execute(ddl)
            conn.execute("PRAGMA user_version=11")
        import owner_slippage
        owner_slippage.apply_on_startup(conn)
        if version < 12:
            conn.execute("PRAGMA user_version=12")
        conn.commit()
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()) -> None:
    conn = connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def exclusive(work):
    """Run work(conn) inside one immediate transaction. work may read and write."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = work(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def set_setting(key: str, value: str, who: str | None = None) -> None:
    """Update one setting and append change_log when the value changes."""
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        old = None if row is None else row["value"]
        if row is not None and old == value:
            return
        now = time.time()
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        conn.execute(
            'INSERT INTO change_log (ts, key, "old", "new", who) VALUES (?, ?, ?, ?, ?)',
            (now, key, old, value, who),
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    rows = query("SELECT value FROM settings WHERE key = ?", (key,))
    if rows and rows[0]["value"] is not None:
        return rows[0]["value"]
    if key in SETTINGS_DEFAULTS:
        return SETTINGS_DEFAULTS[key]
    return default


def get_float(key: str) -> float:
    return float(get_setting(key) or 0)


def flag_on(key: str) -> bool:
    return str(get_setting(key, "")).strip().lower() in {"1", "true", "on", "yes"}


def agents_enabled() -> bool:
    if os.environ.get("DESK_AGENTS") == "1":
        return True
    try:
        return get_setting("agents_enabled", "0") == "1"
    except sqlite3.Error:
        return False


def beat(agent: str, ts: float | None = None) -> None:
    now = time.time() if ts is None else ts
    execute(
        """
        INSERT INTO heartbeats (agent, ts) VALUES (?, ?)
        ON CONFLICT(agent) DO UPDATE SET ts = excluded.ts
        """,
        (agent, now),
    )


def note_ws_reconnect(now: float | None = None, kind: str = "reconnect") -> None:
    """One real PumpPortal reconnect. The first connection is not an event."""
    execute(
        "INSERT INTO ws_events (ts, kind) VALUES (?, ?)",
        (time.time() if now is None else now, kind),
    )


def ws_reconnects_since(start: float) -> int:
    rows = query(
        "SELECT COUNT(*) AS n FROM ws_events WHERE kind = 'reconnect' AND ts >= ?",
        (start,),
    )
    return int(rows[0]["n"] or 0)


def heartbeat_ages(now: float | None = None) -> dict:
    """Seconds since each agent's last beat. None if that agent has never beaten."""
    now = time.time() if now is None else now
    ages: dict[str, float | None] = {name: None for name in AGENT_NAMES}
    for row in query("SELECT agent, ts FROM heartbeats"):
        if row["ts"] is None:
            continue
        ages[str(row["agent"])] = max(0.0, now - float(row["ts"]))
    return ages


def record_activity(ts, kind, mint, agent, title, subtitle, value) -> None:
    execute(
        """
        INSERT INTO activity (ts, kind, mint, agent, title, subtitle, value)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ts, kind, mint, agent, title, subtitle, None if value is None else str(value)),
    )


def db_status() -> dict:
    """Health fields only. No path, no rows, no secrets."""
    conn = connect()
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchone()[0]
        path_ok = _db_file().is_file()
    finally:
        conn.close()
    return {
        "path_ok": bool(path_ok),
        "journal_mode": str(journal_mode).lower(),
        "tables": int(tables),
        "user_version": int(user_version),
    }
