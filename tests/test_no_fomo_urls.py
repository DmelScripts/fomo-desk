"""No FOMO URL is written outside links.py. Tests may mention the host."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", "logs", "node_modules"}
TEXT = {".py", ".js", ".html", ".json", ".css", ".md", ".txt", ".yaml", ".yml"}


class TestNoFomoUrls(unittest.TestCase):
    def test_fomo_family_only_in_links(self):
        found = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP for part in path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel == "links.py" or rel.startswith("tests/"):
                continue
            if path.suffix not in TEXT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "fomo.family" in text:
                found.append(rel)
        self.assertEqual(found, [])
