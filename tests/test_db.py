"""STEP 4 / test_db."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db

TABLES = {
    "coins": (
        "mint", "name", "symbol", "pool", "migrated_at", "creator",
        "creator_balance_at_migration", "holders",
    ),
    "scans": (
        "id", "mint", "ts", "liquidity_usd", "top10_pct", "h1_change_pct",
        "age_min", "verdict", "reason",
    ),
    "audits": (
        "id", "mint", "ts", "layer", "name", "value_text", "pass", "source",
    ),
    "verdicts": (
        "id", "mint", "ts", "alive_6h", "organic", "flush", "chase", "score",
        "pass", "reason", "input_tokens",
    ),
    "signals": (
        "id", "mint", "ts", "price", "size_usd", "stack_pct", "link", "mode", "status",
    ),
    "positions": (
        "id", "mint", "opened_at", "entry_price", "size_usd", "peak_price",
        "stop_price", "trail_active", "closed_at", "exit_price", "exit_reason",
        "pnl_usd", "fees_usd", "mode",
    ),
    "activity": (
        "id", "ts", "kind", "mint", "agent", "title", "subtitle", "value",
    ),
    "prices": ("mint", "ts", "price", "volume_m5"),
    "balance": ("ts", "value"),
    "settings": ("key", "value", "updated_at"),
    "change_log": ("ts", "key", "old", "new", "who"),
    "skips": ("mint", "until"),
    "heartbeats": ("agent", "ts"),
    "push_subs": ("endpoint", "p256dh", "auth", "created_at"),
}

TIMESTAMP_COLUMNS = {
    "migrated_at", "ts", "opened_at", "closed_at", "until", "created_at", "updated_at",
}
BOOLEAN_COLUMNS = {"pass", "alive_6h", "organic", "flush", "chase", "trail_active"}
ID_TABLES = ("scans", "audits", "verdicts", "signals", "positions", "activity")
PRIMARY_KEYS = {
    "coins": "mint",
    "settings": "key",
    "heartbeats": "agent",
    "push_subs": "endpoint",
}
INDEXES = {
    "prices": ("mint", "ts"),
    "scans": ("mint", "ts"),
    "audits": ("mint", "ts"),
    "verdicts": ("mint", "ts"),
    "activity": ("ts",),
    "positions": ("closed_at",),
    "balance": ("ts",),
    "heartbeats": ("agent",),
}


class TestDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = os.environ.get("DESK_DATA_DIR")

        def restore():
            if previous is None:
                os.environ.pop("DESK_DATA_DIR", None)
            else:
                os.environ["DESK_DATA_DIR"] = previous

        self.addCleanup(restore)
        os.environ["DESK_DATA_DIR"] = self.tmp.name

    def test_init_twice_schema_and_pragmas(self):
        db.init_db()
        db.init_db()
        conn = db.connect()
        try:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(names, set(TABLES))
            for table, columns in TABLES.items():
                info = list(conn.execute(f"PRAGMA table_info({table})"))
                self.assertEqual(tuple(row["name"] for row in info), columns)
                by_name = {row["name"]: row for row in info}
                for column, meta in by_name.items():
                    if column in TIMESTAMP_COLUMNS:
                        self.assertEqual(meta["type"], "REAL", table + "." + column)
                    if column in BOOLEAN_COLUMNS:
                        self.assertEqual(meta["type"], "INTEGER", table + "." + column)
                if table in ID_TABLES:
                    self.assertEqual(by_name["id"]["pk"], 1)
                    self.assertEqual(by_name["id"]["type"], "INTEGER")
                    ddl = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()["sql"]
                    self.assertIn("AUTOINCREMENT", ddl.upper())
                pk_column = PRIMARY_KEYS.get(table)
                if pk_column:
                    self.assertEqual(by_name[pk_column]["pk"], 1)
            for table, columns in INDEXES.items():
                found = []
                for index in conn.execute(f"PRAGMA index_list({table})"):
                    cols = [
                        row["name"]
                        for row in conn.execute(f"PRAGMA index_info({index['name']})")
                    ]
                    found.append(tuple(cols))
                self.assertIn(columns, found)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertTrue((Path(self.tmp.name) / "desk.db").is_file())
        finally:
            conn.close()

    def test_kind_and_mode_checks(self):
        db.init_db()
        db.execute(
            "INSERT INTO activity (ts, kind) VALUES (?, ?)",
            (1.0, "bought"),
        )
        db.execute("INSERT INTO signals (ts, mode) VALUES (?, ?)", (1.0, "PAPER"))
        db.execute(
            "INSERT INTO positions (opened_at, mode) VALUES (?, ?)",
            (1.0, "LIVE"),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO activity (ts, kind) VALUES (?, ?)",
                (1.0, "held"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO signals (ts, mode) VALUES (?, ?)", (1.0, "paper"))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO positions (opened_at, mode) VALUES (?, ?)",
                (1.0, "SIM"),
            )

    def test_settings_seed_does_not_overwrite(self):
        db.init_db()
        before = db.query("SELECT key, value FROM settings ORDER BY key")
        keys = [row["key"] for row in before]
        self.assertEqual(keys, sorted(set(keys)))
        self.assertIn("fomo_handle", keys)
        self.assertIn("analyst_mode", keys)
        self.assertIn("rpc_url", keys)
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        db.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            ("kept", "fomo_handle"),
        )
        db.init_db()
        handle = db.query("SELECT value FROM settings WHERE key = ?", ("fomo_handle",))
        self.assertEqual(handle[0]["value"], "kept")
        self.assertEqual(len(db.query("SELECT key FROM settings")), len(before))

    def test_set_setting_writes_change_log(self):
        db.init_db()
        db.set_setting("fomo_handle", "realdmel", "owner")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM change_log")[0]["n"], 0)
        db.set_setting("fomo_handle", "other", "owner")
        rows = db.query('SELECT key, "old", "new", who FROM change_log')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "fomo_handle")
        self.assertEqual(rows[0]["old"], "realdmel")
        self.assertEqual(rows[0]["new"], "other")
        self.assertEqual(rows[0]["who"], "owner")
        self.assertEqual(
            db.query("SELECT value FROM settings WHERE key = ?", ("fomo_handle",))[0]["value"],
            "other",
        )
