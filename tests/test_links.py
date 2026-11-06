"""STEP 13 / test_links."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from links import CHAIN_ID, SIGNUP_URL, coin_url, profile_url

MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"

class TestLinks(unittest.TestCase):
    def test_signup(self):
        self.assertEqual(SIGNUP_URL, "https://fomo.family/r/savipww")

    def test_chain_ids(self):
        self.assertEqual(CHAIN_ID, {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56})

    def test_coin_url_solana(self):
        self.assertEqual(coin_url(MINT),
            f"https://fomo.family/coin?address={MINT}&chainId=1399811149&r=savipww&source=share_link")

    def test_coin_url_other_chains(self):
        for c, i in CHAIN_ID.items():
            u = coin_url(MINT, c)
            self.assertIn(f"chainId={i}", u)
            self.assertIn("r=savipww", u)

    def test_unknown_chain(self):
        for c in ("ethereum", "bsc", "", None):
            self.assertEqual(coin_url(MINT, c), SIGNUP_URL)

    def test_profile(self):
        self.assertEqual(profile_url("@realdmel"),
            "https://fomo.family/profile/realdmel?source=share_link&r=savipww")

if __name__ == "__main__":
    unittest.main()
