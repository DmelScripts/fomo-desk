"""THE ONLY place FOMO URLs are built (STEP 6, non-negotiable).

The spec writes these as raw f-strings, so the query is not percent-encoded
and the parameter order is fixed. r=savipww is on every coin and profile
URL. An unknown chain is logged and replaced with SIGNUP_URL, which carries
the referral as /r/savipww. Nothing here shortens or wraps the URL.
"""
import logging

log = logging.getLogger("links")

CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
SIGNUP_URL = "https://fomo.family/r/savipww"


def coin_url(address, chain="solana"):
    if chain not in CHAIN_ID:
        log.warning("coin_url: unknown chain %r for %s, returning SIGNUP_URL", chain, address)
        return SIGNUP_URL
    return f"https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}&r=savipww&source=share_link"


def profile_url(handle):
    h = handle.lstrip("@")
    return f"https://fomo.family/profile/{h}?source=share_link&r=savipww"
