"""Pre-sign fee estimate. It is not a guaranteed cap and it does not sign."""
import base64
import hashlib
import os
import unittest
from pathlib import Path

import db
import desk_loop
import jupiter_route_v2
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

    def _message(self, values, source, dest, tip, *, side="buy", amount=4, quoted=100, slippage=50):
        return _tx(
            _metas(source, dest, values), _route(quoted, slippage, True, amount=amount),
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

    def test_supervised_retry_and_exit_booking_use_a_fresh_gate(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        encoded = self._message(values, solana_tx.NATIVE_MINT, mint, 395_001)
        self._store(encoded, values["pool"], "entry", "supervised-case")
        reads = {"pool": 0}

        def transport(request):
            if request.get("method") == "getAccountInfo" and request.get("purpose") == "whirlpool_pool":
                reads["pool"] += 1
            if request.get("kind") == "http":
                return {"status": 500, "body": None}
            return self._transport(accounts)(request)

        first = desk_loop.run_supervised(transport, NOW, rounds=1)
        item = next(row for row in first["discovery"]["reservations"] if row["case_id"] == "supervised-case")
        self.assertTrue(item["pre_sign"]["fee_estimate_blocked"], item["pre_sign"])
        self.assertFalse(item["pre_sign"]["signer_reached"])
        self.assertFalse(item["sign"])
        self.assertFalse(item["pre_sign"]["sign"])
        self.assertEqual(item["pre_sign"]["reason"], "fee_estimate_above_0.05")
        self.assertGreaterEqual(reads["pool"], 1)
        seen = reads["pool"]
        accounts[values["pool"]] = (accounts[values["pool"]][0], bytearray(accounts[values["pool"]][1]))
        accounts[values["pool"]][1][solana_whirlpool._TICK_CURRENT - 32:solana_whirlpool._TICK_CURRENT - 16] = (10**24).to_bytes(16, "little")
        second = desk_loop.run_supervised(transport, NOW, rounds=1)
        retry = next(row for row in second["discovery"]["reservations"] if row["case_id"] == "supervised-case")
        self.assertGreater(reads["pool"], seen)
        self.assertEqual(retry["pre_sign"]["fee_estimate"]["attempt"], item["pre_sign"]["fee_estimate"]["attempt"] + 1)
        self.assertNotEqual(retry["pre_sign"]["fee_estimate"]["dex_fee_quote_raw"], item["pre_sign"]["fee_estimate"]["dex_fee_quote_raw"])
        self.assertFalse(retry["pre_sign"]["signer_reached"])
        blind = desk_loop.run_supervised(lambda request: {"status": 500, "body": None} if request.get("kind") == "http" else self._transport(accounts, priced=False)(request), NOW, rounds=1)
        missing = next(row for row in blind["discovery"]["reservations"] if row["case_id"] == "supervised-case")
        self.assertTrue(missing["pre_sign"]["fee_estimate_blocked"], missing["pre_sign"])
        self.assertFalse(missing["pre_sign"]["signer_reached"])
        self.assertNotEqual(missing["pre_sign"]["reason"], "signer_unimplemented")

        exit_values, exit_accounts = _pool(mint, solana_tx.NATIVE_MINT, liquidity=10**18)
        exit_state = solana_whirlpool.pool_state(bytes(exit_accounts[exit_values["pool"]][1]))
        exit_arrays = []
        for address in exit_values["ticks"]:
            parsed = solana_whirlpool.tick_array_state(bytes(exit_accounts[address][1]))
            exit_arrays.append({"start_tick_index": parsed["start_tick_index"], "ticks": parsed["ticks"]})
        exit_est = quote.quote_exact_in(exit_state, exit_arrays, 4, True, 0, 0, 1)["token_est_out"]
        exit_message = self._message(exit_values, mint, solana_tx.NATIVE_MINT, 395_001, side="exit", quoted=exit_est, slippage=0)
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('exit-book', ?, 'partial', ?, ?, ?, 'paper', 'server')",
            (mint, '{"remaining_raw": 4}', NOW, NOW),
        )
        snapshot = jupiter_route_v2.Snapshot(exit_accounts, SLOT, SLOT, NOW)
        snapshot.server_chain = {
            "bind_ok": True,
            "blockhash": solana_tx.b58encode(b"\x44" * 32),
            "router": "metis",
            "last_valid_block_height": 100,
            "last_valid_provenance": "test",
        }
        booked = memecoin_desk.reserve_exit("exit-book", exit_message, 4, snapshot, self._transport(exit_accounts), NOW)
        self.assertTrue(booked.get("ok"), booked)
        self.assertTrue(booked["pre_sign"]["fee_estimate_blocked"], booked["pre_sign"])
        self.assertFalse(booked["pre_sign"]["signer_reached"])
        self.assertFalse(booked["sign"])
        self.assertEqual(booked["pre_sign"]["reason"], "fee_estimate_above_0.05", booked["pre_sign"])
        self.assertEqual(booked["pre_sign"]["fee_estimate"]["pool_read_slot"], SLOT, booked["pre_sign"]["fee_estimate"])
        self.assertEqual(booked["pre_sign"]["fee_estimate"]["pool_sqrt_price"], quote.sqrt_price_from_tick(0))
        self.assertIsNotNone(booked["pre_sign"]["fee_estimate"]["pool_snapshot_sha256"])

    def test_mock_sign_and_send_wait_for_every_earlier_gate(self):
        import phantom_transport
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"mock": True, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        over = self._message(values, solana_tx.NATIVE_MINT, mint, 395_001)
        over_digest = self._store(over, values["pool"], "entry", "gate-over")
        blocked = desk_loop.approach_signer("gate-over", over_digest, self._transport(accounts), NOW, auth)
        self.assertTrue(blocked["fee_estimate_blocked"], blocked)
        self.assertFalse(blocked["signer_reached"])
        self.assertIsNone(blocked["signer"])
        self.assertIsNone(blocked["sender"])
        self.assertFalse(blocked["sign"])
        self.assertFalse(blocked["network_called"])

        wide = self._message(values, solana_tx.NATIVE_MINT, mint, 0, slippage=51)
        wide_digest = self._store(wide, values["pool"], "entry", "gate-slip")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'gate-slip'", ('{"reserved_usd": "8.00"}',))
        slipped = desk_loop.approach_signer("gate-slip", wide_digest, self._transport(accounts), NOW, auth)
        self.assertFalse(slipped["fee_estimate_blocked"], slipped)
        self.assertEqual(slipped["reason"], "slippage_exceeds")
        self.assertFalse(slipped["signer_reached"])
        self.assertIsNone(slipped["sender"])

        db.set_setting("memecoin_entry_halt", "1", "test")
        halted_message = self._message(values, solana_tx.NATIVE_MINT, mint, 0)
        halted_digest = self._store(halted_message, values["pool"], "entry", "gate-halt")
        halted = desk_loop.approach_signer("gate-halt", halted_digest, self._transport(accounts), NOW, auth)
        self.assertEqual(halted["reason"], "entry_halt", halted)
        self.assertIsNone(halted["signer"])
        db.set_setting("memecoin_entry_halt", "0", "test")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'gate-halt'", ('{"reserved_usd": "8.00"}',))

        wrong = dict(auth)
        wrong["wallet_id"] = "not-the-server-wallet"
        mismatched = desk_loop.approach_signer("gate-halt", halted_digest, self._transport(accounts), NOW, wrong)
        self.assertEqual(mismatched["reason"], "wallet_mismatch", mismatched)
        self.assertIsNone(mismatched["signer"])

        passed = desk_loop.approach_signer("gate-halt", halted_digest, self._transport(accounts), NOW, auth)
        self.assertTrue(passed["signer_reached"], passed)
        self.assertTrue(passed["signer"]["signed"])
        self.assertTrue(passed["signer"]["mock"])
        self.assertFalse(passed["signer"]["network_called"])
        self.assertEqual(passed["signer"]["wallet"], phantom_transport.REQUIRED_WALLET)
        self.assertEqual(passed["signer"]["wallet_id"], phantom_transport.REQUIRED_WALLET_ID)
        self.assertTrue(passed["sender"]["accepted"])
        self.assertFalse(passed["sender"]["broadcast"])
        self.assertFalse(passed["sender"]["network_called"])
        self.assertFalse(passed["sign"])
        self.assertFalse(passed["send"])
        self.assertFalse(passed["live_ready"])
        self.assertFalse(passed["ticket_limit_proven"])
        self.assertFalse(passed["allocation_proven"])
        self.assertFalse(desk_loop.readiness()["sign"])
        self.assertFalse(desk_loop.scheduler_status()["send"])

    def test_an_unknown_signature_hold_never_reaches_the_signer(self):
        import phantom_transport
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"mock": True, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        encoded = self._message(values, solana_tx.NATIVE_MINT, mint, 0)
        digest = self._store(encoded, values["pool"], "entry", "unknown-live")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'unknown-live'", ('{"reserved_usd": "8.00"}',))
        db.execute(
            "INSERT INTO memecoin_reservation_signatures (signature, message_sha256, case_id, kind, created_at) VALUES ('sig-unknown', ?, 'unknown-live', 'unknown', ?)",
            (digest, NOW),
        )
        held = desk_loop.approach_signer("unknown-live", digest, self._transport(accounts), NOW, auth)
        self.assertEqual(held["reason"], "unknown_signature_hold", held)
        self.assertFalse(held["signer_reached"])
        self.assertIsNone(held["signer"])
        self.assertIsNone(held["sender"])
        self.assertFalse(held["sign"])
        self.assertFalse(held["send"])

        paper = self._message(values, solana_tx.NATIVE_MINT, mint, 0, slippage=0, quoted=100)
        paper_digest = hashlib.sha256(solana_tx.message_bytes(paper)).hexdigest()
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('unknown-paper', ?, 'protected', ?, ?, ?, 'paper', 'server')",
            (mint, '{"reserved_usd": "8.00"}', NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash,
                last_valid_block_height, last_valid_provenance, pool, ledger, ledger_provenance, created_at
            ) VALUES (?, 'unknown-paper', 'entry', ?, 'metis', ?, 100, 'test', ?, 'paper', 'server', ?)
            """,
            (paper_digest, paper, solana_tx.b58encode(b"\x44" * 32), values["pool"], NOW),
        )
        db.execute(
            "INSERT INTO memecoin_reservation_signatures (signature, message_sha256, case_id, kind, created_at) VALUES ('sig-paper-unknown', ?, 'unknown-paper', 'unknown', ?)",
            (paper_digest, NOW),
        )
        self.assertEqual(db.exclusive(lambda conn: memecoin_desk._reservation_status(conn, db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (paper_digest,))[0])), "active")
        paper_held = desk_loop.approach_signer("unknown-paper", paper_digest, self._transport(accounts), NOW, auth)
        self.assertEqual(paper_held["reason"], "unknown_signature_hold", paper_held)
        self.assertIsNone(paper_held["signer"])
        self.assertIsNone(paper_held["sender"])

    def test_the_reservation_ticket_and_allocation_bound_the_signer(self):
        import phantom_transport
        mint = solana_tx.b58encode(b"\x74" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"mock": True, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        encoded = self._message(values, solana_tx.NATIVE_MINT, mint, 0)
        digest = self._store(encoded, values["pool"], "entry", "ticket-case")
        transport = self._transport(accounts)

        def at(amount):
            db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'ticket-case'", ('{"reserved_usd": "%s"}' % amount,))
            return desk_loop.approach_signer("ticket-case", digest, transport, NOW, auth)

        passed = at("8.00")
        self.assertTrue(passed["signer_reached"], passed)
        self.assertIsNotNone(passed["sender"])
        over_cent = at("8.01")
        self.assertEqual(over_cent["reason"], "ticket", over_cent)
        self.assertIsNone(over_cent["signer"])
        self.assertIsNone(over_cent["sender"])
        large = at("50.00")
        self.assertEqual(large["reason"], "ticket")
        self.assertIsNone(large["signer"])
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('other-hold', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (mint, '{"reserved_usd": "92.00"}', NOW, NOW),
        )
        boundary = at("8.00")
        self.assertTrue(boundary["signer_reached"], boundary)
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'other-hold'", ('{"reserved_usd": "92.01"}',))
        crowded = at("8.00")
        self.assertEqual(crowded["reason"], "allocation", crowded)
        self.assertIsNone(crowded["signer"])
        self.assertIsNone(crowded["sender"])

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
        def refuse_price(request):
            raise AssertionError(request.get("method"))

        compared = desk_loop.log_landed_fee_against_estimate("landed-case", signature, refuse_price, NOW)
        self.assertTrue(compared["logged"], compared)
        self.assertTrue(compared["overrun"], compared)
        self.assertEqual(compared["label"], "estimate, not guaranteed")
        self.assertFalse(compared["hard_cap"])
        self.assertFalse(compared["repriced"])
        self.assertGreater(compared["landed_dex_raw"], compared["estimate_dex_raw"])
        self.assertEqual(compared["landed_network_lamports"], compared["estimate_network_lamports"])
        row = db.query("SELECT source, evidence_json FROM memecoin_log WHERE case_id = 'landed-case' AND verdict = 'landed'")[0]
        self.assertEqual(row["source"], "estimate, not guaranteed")
        self.assertIn('"overrun": true', row["evidence_json"])
