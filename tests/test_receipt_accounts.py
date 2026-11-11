"""Cost basis and rent from a parsed transaction. No live read."""
import base64
import json
import unittest
from pathlib import Path

import memecoin_exit
import phantom_transport
import solana_tx
import stalkchain_observed
from tests.test_jupiter_route_v2 import _metas, _route, _tx

WALLET = phantom_transport.REQUIRED_WALLET
MINT = solana_tx.b58encode(b"\x73" * 32)
INPUT = 1_000_000
FEE = 5_000
DEPOSIT = 1_488_440
LEFT = 20_000_000


def _buy():
    from tests.test_fee_estimate import _pool
    values, _accounts = _pool(solana_tx.NATIVE_MINT, MINT, liquidity=10**18)
    encoded = _tx(
        _metas(solana_tx.NATIVE_MINT, MINT, values),
        _route(100, 50, True, amount=INPUT),
        side="buy",
        fund_mint=MINT,
        blockhash=solana_tx.b58encode(b"\x44" * 32),
    )
    raw = bytearray(base64.b64decode(encoded))
    raw[1:65] = b"\x11" * 64
    encoded = base64.b64encode(bytes(raw)).decode()
    decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
    wsol = solana_tx.associated_token_address(WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    mint_ata = solana_tx.associated_token_address(WALLET, MINT, solana_tx.TOKEN_PROGRAM)
    pre = [1] * len(decoded.static_keys)
    post = [1] * len(decoded.static_keys)
    pre[0] = LEFT + INPUT + FEE + DEPOSIT
    post[0] = LEFT
    pre[decoded.static_keys.index(wsol)] = 0
    post[decoded.static_keys.index(wsol)] = 0
    pre[decoded.static_keys.index(mint_ata)] = 0
    post[decoded.static_keys.index(mint_ata)] = DEPOSIT
    meta = {
        "err": None,
        "fee": FEE,
        "preBalances": pre,
        "postBalances": post,
        "preTokenBalances": [],
        "postTokenBalances": [{
            "accountIndex": decoded.static_keys.index(mint_ata),
            "mint": MINT,
            "owner": WALLET,
            "uiTokenAmount": {"amount": "1000", "decimals": 6},
        }],
    }
    reserved = {
        "message_sha256": __import__("hashlib").sha256(solana_tx.message_bytes(encoded)).hexdigest(),
        "account_keys": list(decoded.static_keys),
        "mint": MINT,
        "input_lamports": INPUT,
        "min_out": 1,
        "destination_account": mint_ata,
        "destination_index": decoded.static_keys.index(mint_ata),
        "snapshot_slot": 10,
        "snapshot_tip_slot": 10,
        "blockhash": decoded.blockhash,
        "pool": values["pool"],
        "dex_fee_lamports": 0,
        "snapshot_fetched_at": 1,
    }
    return encoded, decoded, meta, reserved, mint_ata


class ReceiptAccountTests(unittest.TestCase):
    def test_parsed_balances_prove_cost_basis_and_leave_the_deposit_open(self):
        encoded, _decoded, meta, reserved, mint_ata = _buy()
        proved = memecoin_exit.match_entry(reserved, solana_tx.first_signature(encoded), {
            "ok": True, "transaction": encoded, "slot": 12, "meta": meta, "block_time": None,
        })
        self.assertTrue(proved["ok"], proved)
        self.assertTrue(proved["cost_basis_proven"])
        self.assertEqual(proved["cost_basis_lamports"], INPUT + FEE)
        self.assertEqual(proved["deposit_lamports"], DEPOSIT)
        self.assertNotEqual(proved["cost_basis_lamports"], proved["wallet_spent_lamports"])
        self.assertTrue(proved["rent_bound_proven"])
        self.assertEqual(proved["rent_bound_lamports"], DEPOSIT)
        self.assertFalse(proved["deposit_refund_proven"])
        self.assertTrue(proved["deposit_exposure_open"])
        self.assertFalse(proved["preexisting_close_refused"])
        self.assertFalse(proved["rent"]["mint_ata"]["preexisting"])

    def test_a_preexisting_account_is_not_closed_or_treated_as_new_rent(self):
        encoded, decoded, meta, reserved, mint_ata = _buy()
        index = decoded.static_keys.index(mint_ata)
        meta["preBalances"][index] = DEPOSIT
        meta["postBalances"][index] = DEPOSIT
        meta["preBalances"][0] -= DEPOSIT
        meta["preTokenBalances"] = [{
            "accountIndex": index, "mint": MINT, "owner": WALLET,
            "uiTokenAmount": {"amount": "0", "decimals": 6},
        }]
        proved = memecoin_exit.match_entry(reserved, solana_tx.first_signature(encoded), {
            "ok": True, "transaction": encoded, "slot": 12, "meta": meta, "block_time": None,
        })
        self.assertTrue(proved["ok"], proved)
        self.assertEqual(proved["deposit_lamports"], 0)
        self.assertFalse(proved["rent_bound_proven"])
        self.assertFalse(proved["deposit_exposure_open"])
        self.assertFalse(proved["rent"]["mint_ata"]["user_account_closed"])

    def test_a_refund_of_the_created_account_clears_the_exposure(self):
        _encoded, _decoded, _meta, _reserved, mint_ata = _buy()
        recorded = {
            "account": mint_ata,
            "rent_lamports": DEPOSIT,
            "preexisting": False,
            "bot_funded": True,
            "created_in_message": True,
        }
        unsigned_close = solana_tx.encode_legacy(
            WALLET,
            [WALLET, mint_ata, solana_tx.TOKEN_PROGRAM],
            1,
            [(2, [1, 0, 0], bytes([9]))],
            solana_tx.b58encode(b"\x45" * 32),
        )
        close_raw = bytearray(base64.b64decode(unsigned_close))
        close_raw[1:65] = b"\x22" * 64
        close = base64.b64encode(bytes(close_raw)).decode()
        refund_meta = {
            "fee": FEE,
            "preBalances": [LEFT, DEPOSIT, 1],
            "postBalances": [LEFT + DEPOSIT - FEE, 0, 1],
        }
        refunded = memecoin_exit.match_refund(recorded, solana_tx.first_signature(close), {
            "ok": True, "transaction": close, "slot": 20, "meta": refund_meta,
        })
        self.assertTrue(refunded["ok"], refunded)
        self.assertTrue(refunded["refunded"])
        preexisting = dict(recorded)
        preexisting["preexisting"] = True
        refused = memecoin_exit.match_refund(preexisting, solana_tx.first_signature(close), {
            "ok": True, "transaction": close, "slot": 20, "meta": refund_meta,
        })
        self.assertEqual(refused["reason"], "preexisting_account")
        self.assertFalse(refused["refunded"])

    def test_the_recorded_capture_has_no_lock_expiry_or_rug_history(self):
        gaps = stalkchain_observed.documented_measurement_gaps()
        self.assertFalse(gaps["lp_lock_expiry"]["available"])
        self.assertFalse(gaps["rug_history"]["available"])
        self.assertFalse(gaps["holder_growth"]["available"])
        fixture = json.loads(Path("tests/fixtures/nov8_observed_mcp_fixtures.json").read_text())
        onchain = fixture["responses"]["onchain"]["data"]
        deployer = fixture["responses"]["deployer"]["data"]
        self.assertNotIn("lockExpiry", onchain)
        self.assertNotIn("unlockTime", onchain)
        self.assertEqual(onchain["lpBurnedPercent"], 91)
        self.assertNotIn("rugged", deployer["tokens"][0])
        self.assertEqual(deployer["tokensLaunched"], 1)
        series = stalkchain_observed.holder_growth_series([
            {"source": "onchain", "mint": onchain["mint"], "metric": "holdersTotal", "captured_at": "2026-11-08T06:11:39.000Z", "value": onchain["holdersTotal"]},
            {"source": "snapshot", "mint": onchain["mint"], "metric": "totalHolders", "captured_at": "2026-11-08T06:56:01.771Z", "value": 1377},
        ])
        self.assertFalse(series["ok"])
        self.assertTrue(series["cross_source_is_growth"])
        same = stalkchain_observed.holder_growth_series([
            {"source": "onchain", "mint": onchain["mint"], "metric": "holdersTotal", "captured_at": "2026-11-08T06:11:39.000Z", "value": 1500},
            {"source": "onchain", "mint": onchain["mint"], "metric": "holdersTotal", "captured_at": "2026-11-08T07:11:39.000Z", "value": 1588},
        ])
        self.assertTrue(same["ok"], same)
        self.assertEqual(same["delta"], 88)
        self.assertFalse(same["score_measured"])
