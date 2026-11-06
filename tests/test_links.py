"""STEP 6. Every FOMO URL is the exact links.py string, referral included."""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from links import CHAIN_ID, SIGNUP_URL, coin_url, profile_url
from notifier import Notifier

# Public sample mint and an example handle. Neither is a secret.
MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"
HANDLE = "example"

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", "logs", "node_modules"}
APP_TEXT = {".py", ".js", ".html", ".json", ".css", ".md", ".txt", ".yaml", ".yml"}
SHORTENERS = ("bit.ly", "t.co", "tinyurl.com", "goo.gl")


def coin(chain):
    return (
        "https://fomo.family/coin?address="
        + MINT
        + "&chainId="
        + str(CHAIN_ID[chain])
        + "&r=savipww&source=share_link"
    )


class TestLinks(unittest.TestCase):
    def test_signup_carries_the_referral(self):
        self.assertEqual(SIGNUP_URL, "https://fomo.family/r/savipww")
        self.assertIn("/r/savipww", SIGNUP_URL)

    def test_chain_ids(self):
        self.assertEqual(CHAIN_ID, {
            "solana": 1399811149,
            "robinhood": 4663,
            "base": 8453,
            "bnb": 56,
        })

    def test_coin_url_exact_for_every_chain(self):
        self.assertEqual(coin_url(MINT), coin("solana"))
        self.assertEqual(coin_url(MINT, "solana"), coin("solana"))
        for chain in CHAIN_ID:
            url = coin_url(MINT, chain)
            self.assertEqual(url, coin(chain))
            self.assertIn("r=savipww", url)
            self.assertTrue(url.startswith("https://fomo.family/coin?"))
            self.assertNotIn("bit.ly", url)
            self.assertNotIn("t.co", url)
        # pumpfun coins are Solana.
        self.assertIn("chainId=1399811149", coin_url(MINT, "solana"))

    def test_unknown_chain_logs_and_returns_signup(self):
        for chain in ("ethereum", "bsc", "Solana", "", None):
            with self.assertLogs("links", level="WARNING") as caught:
                self.assertEqual(coin_url(MINT, chain), SIGNUP_URL)
            self.assertTrue(any(repr(chain) in line for line in caught.output))

    def test_profile_strips_one_at_and_keeps_referral(self):
        expected = "https://fomo.family/profile/example?source=share_link&r=savipww"
        self.assertEqual(profile_url(HANDLE), expected)
        self.assertEqual(profile_url("@" + HANDLE), expected)
        self.assertEqual(profile_url("@@" + HANDLE), expected)
        self.assertIn("r=savipww", profile_url(HANDLE))

    def test_button_uses_the_exact_string_and_plain_text_keeps_it(self):
        url = coin_url(MINT, "solana")
        notifier = Notifier(mode="PAPER")
        notifier.push("Buy EX", "sized", url, [{"label": "Buy on FOMO", "url": url}])
        self.assertEqual(notifier.pushes[0]["buttons"][0]["url"], url)
        self.assertEqual(notifier.pushes[0]["url"], url)
        self.assertNotIn(url, notifier.pushes[0]["body"])
        notifier.push("Executor", "night check", SIGNUP_URL, [])
        self.assertTrue(str(notifier.pushes[1]["body"]).endswith("\n" + SIGNUP_URL))
        self.assertEqual(notifier.pushes[1]["url"], SIGNUP_URL)

    def test_application_code_does_not_build_fomo_urls(self):
        """Tests may name the host in assertions. Application code may not."""
        found = []
        shortened = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in SKIP for part in path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel == "links.py" or rel.startswith("tests/"):
                continue
            if path.suffix not in APP_TEXT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "fomo.family" in text:
                found.append(rel)
            for host in SHORTENERS:
                if host in text:
                    shortened.append(rel + ":" + host)
        self.assertEqual(found, [])
        self.assertEqual(shortened, [])
        source = (ROOT / "links.py").read_text(encoding="utf-8")
        self.assertIn("r=savipww", source)
        for host in SHORTENERS:
            self.assertNotIn(host, source)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
