"""Run the fixture suite with disposable data and no server wallet session."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    with tempfile.TemporaryDirectory(prefix="desk-offline-") as directory:
        os.environ.update({
            "DESK_DATA_DIR": directory,
            "DESK_SECRETS_PATH": str(Path(directory) / "test-secrets.env"),
            "DESK_PHANTOM_HOME": str(Path(directory) / "phantom-mcp"),
            "DESK_AGENTS": "0",
        })
        for key in ("APP_TOKEN", "TYPESAFE_API_KEY", "VAPID_PRIVATE_KEY", "TG_TOKEN"):
            os.environ.pop(key, None)
        import db
        db.init_db()
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
