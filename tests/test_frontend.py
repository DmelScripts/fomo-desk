"""STEP 8 app shell. Dummy tokens only. No live network."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support import temp_desk_env

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("NODE") or shutil.which("node") or "/exec-daemon/node"
HARNESS = ROOT / "tests" / "frontend_harness.js"


def source(name: str) -> str:
    return (ROOT / "web" / name).read_text(encoding="utf-8")


class TestFrontend(unittest.TestCase):
    def test_shell_has_no_token_and_no_fomo_host(self):
        joined = "\n".join(source(name) for name in (
            "index.html", "app.js", "app.css", "chart.js", "sw.js", "manifest.json",
        ))
        self.assertNotIn("fomo.family", joined)
        self.assertNotIn("EventSource", joined)
        self.assertNotIn("console.", source("app.js"))
        self.assertNotIn("console.", source("chart.js"))
        self.assertIn("apple-touch-icon", source("index.html"))
        self.assertIn("manifest.json", source("index.html"))
        self.assertIn("viewport-fit=cover", source("index.html"))
        self.assertIn("safe-area-inset", source("app.css"))
        self.assertIn("#00d35a", source("app.css"))
        self.assertIn("deskCacheAllowed", source("sw.js"))
        self.assertNotIn("localStorage", source("sw.js"))
        self.assertIn("Bearer", source("app.js"))
        self.assertIn("/api/stream", source("app.js"))
        self.assertIn("/api/push", source("app.js"))
        self.assertIn("/api/mode", source("app.js"))
        self.assertIn("/api/settings", source("app.js"))
        page = source("index.html")
        self.assertIn("Waiting for the first trade", page)
        self.assertIn("Nothing yet tonight", page)
        self.assertIn("No coin taken apart yet", page)

    def test_pages_served_without_a_token(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = "dummy-app-token"
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Grok Bot", page.text)
        self.assertNotIn("dummy-app-token", page.text)
        self.assertIn("no-store", page.headers.get("cache-control", ""))
        script = client.get("/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertNotIn("dummy-app-token", script.text)
        worker = client.get("/sw.js")
        self.assertIn("deskCacheAllowed", worker.text)
        self.assertEqual(client.get("/manifest.json").status_code, 200)

    def test_browser_helpers(self):
        self._node("pure")
        self._node("fragment")
        self._node("query")
        self._node("auth401")
        self._node("sse")
        self._node("sw")

    def _node(self, mode: str):
        binary = NODE if NODE and Path(NODE).exists() else shutil.which("node")
        if not binary:
            self.fail("node is required for the app harness")
        result = subprocess.run(
            [binary, str(HARNESS), mode],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
