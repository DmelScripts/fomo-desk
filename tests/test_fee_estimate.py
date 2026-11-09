"""Pre-sign fee estimate. It is not a guaranteed cap and it does not sign."""
import base64
import hashlib
import os
import unittest
from pathlib import Path

import db
import desk_loop
import memecoin_desk
import phantom_transport
import pyth_sponsored
import solana_tx
import solana_whirlpool
import solana_whirlpool_quote as quote
from tests.support import temp_desk_env
from tests.test_jupiter_route_v2 import _metas, _route, _tx

NOW = 1_700_000_000
SLOT = 12
TIP_ACCOUNT = solana_tx.b58encode(b"\x91" * 32)


def _sol_price(now: int, slot: int) -> bytes:
    raw = bytearray(Path("tests/fixtures/pyth_sol_usd_price_update_v2.bin").read_bytes())
    message = 41
    raw[message + 32:message + 40] = (100).to_bytes(8, "little", signed=True)
    raw[message + 40:message + 48] = (0).to_bytes(8, "little")
    raw[message + 48:message + 52] = (0).to_bytes(4, "little", signed=True)
    raw[message + 52:message + 60] = int(now).to_bytes(8, "little", signed=True)
    raw[message + 84:message + 92] = int(slot).to_bytes(8, "little")
    return bytes(raw)


def _pool(source_mint: str, dest_mint: str, *, fee_rate: int = 3000, liquidity: int = 1, adaptive: bool = False):
    wallet = phantom_transport.REQUIRED_WALLET
    vault_a = solana_tx.b58encode(b"\x31" * 32)
    vault_b = solana_tx.b58encode(b"\x32" * 32)
    raw = bytearray(solana_whirlpool.reference_pool(
        source_mint, vault_a, dest_mint, vault_b, 1, 0,
        sqrt_price=quote.sqrt_price_from_tick(0), fee_rate=fee_rate, liquidity=liquidity,
    ))
    raw[8:40] = b"\x45" * 32
    if adaptive:
        raw[solana_whirlpool._TICK_SPACING + 2] = raw[solana_whirlpool._TICK_SPACING] + 1
    fields = solana_whirlpool.pool_fields(bytes(raw))
    seed = bytes(raw[solana_whirlpool._TICK_SPACING + 2:solana_whirlpool._TICK_SPACING + 4])
    derived, bump = solana_tx.find_program_address(
        [b"whirlpool", bytes(raw[8:40]), solana_tx.b58decode(fields[0]), solana_tx.b58decode(fields[2]), seed],
        solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
    )
    raw[40] = bump
    pool = solana_tx.b58encode(derived)
    starts = [0, -88, -176]
    ticks = []
    accounts = {pool: (solana_whirlpool.PROGRAM_ID, bytes(raw))}
    for start in starts:
        address = solana_whirlpool.tick_array_address(pool, start)
        accounts[address] = (solana_whirlpool.PROGRAM_ID, bytearray(solana_whirlpool.reference_tick_array(pool, start)))
        ticks.append(address)
    crossed = accounts[ticks[1]][1]
    for offset in (86, 87):
        crossed[12 + offset * 113] = 1
    values = {
        "pool": pool,
        "source": solana_tx.associated_token_address(wallet, source_mint, solana_tx.TOKEN_PROGRAM),
        "dest": solana_tx.associated_token_address(wallet, dest_mint, solana_tx.TOKEN_PROGRAM),
        "vault_a": vault_a, "vault_b": vault_b, "ticks": ticks,
        "oracle": solana_whirlpool.oracle_address(pool),
        "authority": wallet, "token_program": solana_tx.TOKEN_PROGRAM,
    }
    return values, accounts


class FeeEstimateTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ.pop("STALKCHAIN_API_KEY", None)
        db.init_db()
        memecoin_desk._ensure()
        pyth_sponsored._LAST = None

    def tearDown(self):
        pyth_sponsored._LAST = None

    def _store(self, encoded, pool, side, case_id="fee-case"):
        digest = hashlib.sha256(solana_tx.message_bytes(encoded)).hexdigest()
        mint = solana_tx.b58encode(b"\x73" * 32)
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?, ?, 'protected', '{}', ?, ?, 'live', 'server')",
            (case_id, mint, NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash,
                last_valid_block_height, last_valid_provenance, pool, ledger, ledger_provenance, created_at
            ) VALUES (?, ?, ?, ?, 'metis', ?, 100, 'test', ?, 'live', 'server', ?)
            """,
            (digest, case_id, side, encoded, solana_tx.b58encode(b"\x44" * 32), pool, NOW),
        )
        return digest

    def _transport(self, accounts, *, slot=SLOT, tip=SLOT, priced=True, now=NOW):
        sol = _sol_price(now, slot)
        usdc = Path("tests/fixtures/pyth_usdc_usd_price_update_v2.bin").read_bytes()

        def transport(request):
            if request.get("method") == "getSlot":
                return {"status": 200, "body": {"result": tip}}
            if request.get("method") == "getMultipleAccounts":
                if not priced:
                    return {"status": 200, "body": {"result": {"context": {"slot": slot}, "value": None}}}
                return {"status": 200, "body": {"result": {
                    "context": {"slot": slot},
                    "value": [
                        {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "executable": False, "data": [base64.b64encode(sol).decode(), "base64"]},
                        {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "executable": False, "data": [base64.b64encode(usdc).decode(), "base64"]},
                    ],
                }}}
            record = accounts.get(request.get("account"))
            value = None if record is None else {
                "owner": record[0], "executable": False, "lamports": 1,
                "data": [base64.b64encode(bytes(record[1])).decode("ascii"), "base64"],
            }
            return {"status": 200, "body": {"result": {"context": {"slot": slot}, "value": value, "blockhash": solana_tx.b58encode(b"\x44" * 32)}}}

        return transport

    def _message(self, values, source, dest, tip, *, side="buy", amount=4):
        return _tx(
            _metas(source, dest, values), _route(100, 50, True, amount=amount),
            limit=200_000, price=0, side=side, extra_transfer=TIP_ACCOUNT, tip_lamports=tip,
            blockhash=solana_tx.b58encode(b"\x44" * 32),
        )

    def test_threshold_boundary_price_staleness_retry_and_network_parts(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        under = self._message(values, solana_tx.NATIVE_MINT, mint, 0)
        digest = self._store(under, values["pool"], "entry")
        low = desk_loop.fee_estimate_before_sign("fee-case", digest, self._transport(accounts), NOW, 1)
        self.assertFalse(low["fee_estimate_skip"], low)
        self.assertFalse(low["sign"])
        self.assertFalse(low["combined_fee_usd_proven"])
        self.assertFalse(low["fee_estimate_is_hard_cap"])
        self.assertEqual(low["fee_estimate_label"], "estimate, not guaranteed")
        self.assertEqual(low["reason"], "signer_unimplemented")
        self.assertEqual(low["dex_fee_quote_raw"], 2)
        self.assertEqual(low["dex_fee_quote_unit"], "lamports")
        self.assertEqual(low["base_fee_lamports"], 5000)
        self.assertEqual(low["priority_lamports"], 0)
        self.assertEqual(low["tip_lamports"], 0)
        self.assertLess(low["estimated_fee_usd"], "0.05")

        exact = self._message(values, solana_tx.NATIVE_MINT, mint, 395_000)
        exact_digest = self._store(exact, values["pool"], "entry", "exact-case")
        boundary = desk_loop.fee_estimate_before_sign("exact-case", exact_digest, self._transport(accounts), NOW, 1)
        self.assertEqual(boundary["estimated_fee_usd"], "0.05", boundary)
        self.assertEqual(boundary["dex_fee_usd"], "0.01")
        self.assertEqual(boundary["network_fee_usd"], "0.04")
        self.assertFalse(boundary["fee_estimate_skip"], boundary)
        self.assertEqual(boundary["network_lamports"], 400_000)

        high = self._message(values, solana_tx.NATIVE_MINT, mint, 395_001)
        high_digest = self._store(high, values["pool"], "entry", "over-case")
        over = desk_loop.fee_estimate_before_sign("over-case", high_digest, self._transport(accounts), NOW, 1)
        self.assertTrue(over["fee_estimate_skip"], over)
        self.assertFalse(over["sign"])
        self.assertEqual(over["reason"], "fee_estimate_above_0.05")
        self.assertGreater(over["estimated_fee_usd"], "0.05")
        logged = db.query("SELECT source, reason, evidence_json FROM memecoin_log WHERE case_id = 'over-case'")[0]
        self.assertEqual(logged["source"], "estimate, not guaranteed")
        self.assertEqual(logged["reason"], "fee_estimate_above_0.05")
        self.assertIn("estimate, not guaranteed", logged["evidence_json"])
        self.assertNotIn("hard cap", logged["evidence_json"])

        missing = desk_loop.fee_estimate_before_sign("over-case", high_digest, self._transport(accounts, priced=False), NOW, 2)
        self.assertTrue(missing["fee_estimate_skip"])
        self.assertFalse(missing["sign"])
        self.assertIn(missing["reason"], {"usd_unpriced", "valuation_stale", "rpc_shape", "account_missing"})

        stale = desk_loop.fee_estimate_before_sign("over-case", high_digest, self._transport(accounts, slot=1, tip=20), NOW, 3)
        self.assertTrue(stale["fee_estimate_skip"], stale)
        self.assertEqual(stale["reason"], "quote_stale")

        accounts[values["pool"]] = (accounts[values["pool"]][0], bytearray(accounts[values["pool"]][1]))
        accounts[values["pool"]][1][solana_whirlpool._TICK_CURRENT - 32:solana_whirlpool._TICK_CURRENT - 16] = (10**24).to_bytes(16, "little")
        again = desk_loop.fee_estimate_before_sign("over-case", high_digest, self._transport(accounts), NOW, 4)
        self.assertEqual(again["attempt"], 4)
        self.assertNotEqual(again["dex_fee_quote_raw"], over["dex_fee_quote_raw"])
        self.assertEqual(again["dex_fee_quote_raw"], 4, again)

    def test_exit_slice_converts_token_units_apart_from_lamports(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
        encoded = self._message(values, mint, solana_tx.NATIVE_MINT, 395_000, side="exit")
        digest = self._store(encoded, values["pool"], "exit", "exit-case")
        checked = desk_loop.fee_estimate_before_sign("exit-case", digest, self._transport(accounts), NOW, 1)
        self.assertEqual(checked["dex_fee_quote_unit"], "token_raw", checked)
        self.assertEqual(checked["dex_fee_quote_raw"], 2)
        self.assertEqual(checked["dex_fee_usd"], "0.01")
        self.assertEqual(checked["network_fee_usd"], "0.04")
        self.assertEqual(checked["estimated_fee_usd"], "0.05")
        self.assertNotEqual(checked["dex_fee_quote_raw"] + checked["network_lamports"], int(float(checked["estimated_fee_usd"]) * 100))

    def test_an_adaptive_pool_is_not_signed(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint, adaptive=True)
        encoded = self._message(values, solana_tx.NATIVE_MINT, mint, 0)
        digest = self._store(encoded, values["pool"], "entry", "adaptive-case")
        checked = desk_loop.fee_estimate_before_sign("adaptive-case", digest, self._transport(accounts), NOW, 1)
        self.assertTrue(checked["fee_estimate_skip"], checked)
        self.assertFalse(checked["sign"])
        self.assertEqual(checked["reason"], "adaptive_fee_unsupported")

    def test_a_landed_fee_above_the_estimate_is_logged(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        encoded = self._message(values, solana_tx.NATIVE_MINT, mint, 0)
        digest = self._store(encoded, values["pool"], "entry", "landed-case")
        estimate = desk_loop.fee_estimate_before_sign("landed-case", digest, self._transport(accounts), NOW, 1)
        self.assertFalse(estimate["fee_estimate_skip"], estimate)
        signature = "sig-overrun"
        wallet = phantom_transport.REQUIRED_WALLET
        db.execute(
            """
            INSERT INTO memecoin_landed_evidence (
                signature, message_sha256, case_id, side, slot, confirmation_status,
                network_fee_lamports, meta_err, wallet, layout_supported, created_at
            ) VALUES (?, ?, 'landed-case', 'entry', 12, 'confirmed', 5000, 'null', ?, 1, ?)
            """,
            (signature, digest, wallet, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_landed_quantity (
                signature, message_sha256, case_id, side, account, mint, wallet, decimals,
                pre_amount, post_amount, quantity_raw, direction, landed_slot, created_at
            ) VALUES (?, ?, 'landed-case', 'entry', ?, ?, ?, 6, '0', '100', '100', 'bought', 12, ?)
            """,
            (signature, digest, values["dest"], mint, wallet, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_landed_flow (
                signature, message_sha256, case_id, side, direction, input_mint, output_mint,
                input_raw, output_raw, proceeds_raw, network_fee_lamports, landed_slot, vault_binding, created_at
            ) VALUES (?, ?, 'landed-case', 'entry', 'bought', ?, ?, '4', '100', NULL, 5000, 12, 'whirlpool_swap_constraint', ?)
            """,
            (signature, digest, solana_tx.NATIVE_MINT, mint, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_landed_swap_fee (
                signature, message_sha256, case_id, side, pool, a_to_b, input_raw, output_raw,
                lp_fee, protocol_fee, fee_unit, network_fee_lamports, pre_sqrt_price, post_sqrt_price, created_at
            ) VALUES (?, ?, 'landed-case', 'entry', ?, 1, '4', '100', '400000', '0', 'lamports', 5000, ?, ?, ?)
            """,
            (signature, digest, values["pool"], str(quote.sqrt_price_from_tick(0)), str(quote.sqrt_price_from_tick(0)), NOW),
        )
        compared = desk_loop.log_landed_fee_against_estimate("landed-case", signature, self._transport(accounts), NOW)
        self.assertTrue(compared["logged"], compared)
        self.assertTrue(compared["overrun"], compared)
        self.assertEqual(compared["label"], "estimate, not guaranteed")
        self.assertFalse(compared["hard_cap"])
        self.assertGreater(compared["landed_fee_usd"], estimate["estimated_fee_usd"])
        row = db.query("SELECT source, evidence_json FROM memecoin_log WHERE case_id = 'landed-case' AND verdict = 'landed'")[0]
        self.assertEqual(row["source"], "estimate, not guaranteed")
        self.assertIn("overrun", row["evidence_json"])
