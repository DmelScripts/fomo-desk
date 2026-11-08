"""Sponsored Solana Pyth accounts are not read by this process.

The Solana guide documents PriceUpdateV2 and getPriceFeedAccountAddress(shard, feedId)
for shard 0. The page fetched for this batch names those SDK entry points and not the
program id bytes, the PDA seeds, or the account layout. The sponsored-feed table's
rows were not in that page. Those addresses are not guessed.

The default sponsored heartbeat on Solana is 1 minute / 0.5 percent. This desk's
price window is 30 seconds. A sponsored account can be older than that window
without being wrong. The window is not widened.

Hermes price requests need a Pyth API key. This module does not call Hermes and
does not push a price.
https://docs.pyth.network/pro/use-prices-on-blockchain/solana
https://docs.pyth.network/pro/sponsored-feeds
https://docs.pyth.network/pro/pyth-core-upgrade
"""

from __future__ import annotations


def status() -> dict:
    return {
        "available": False,
        "reason": "price_update_v2_layout_unverified",
        "usable": False,
        "live_provider": False,
        "fetched": False,
        "account": None,
        "program_id": None,
        "freshness_window_s": 30,
        "sponsored_heartbeat": "1 minute / 0.5 percent default; not widened",
        "hermes_called": False,
        "pusher": False,
    }
