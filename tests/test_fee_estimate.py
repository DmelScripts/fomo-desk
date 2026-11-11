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
    accounts[vault_a] = (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(source_mint, pool))
    accounts[vault_b] = (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(dest_mint, pool))
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

    def _store(self, encoded, pool, side, case_id="fee-case", *, created_at=NOW, last_valid=100, blockhash=None):
        digest = hashlib.sha256(solana_tx.message_bytes(encoded)).hexdigest()
        mint = solana_tx.b58encode(b"\x73" * 32)
        stored_hash = solana_tx.b58encode(b"\x44" * 32) if blockhash is None else blockhash
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?, ?, 'protected', ?, ?, ?, 'live', 'server')",
            (case_id, mint, '{"reserved_usd": "0.00"}', NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash,
                last_valid_block_height, last_valid_provenance, pool, ledger, ledger_provenance, created_at
            ) VALUES (?, ?, ?, ?, 'metis', ?, ?, 'test', ?, 'live', 'server', ?)
            """,
            (digest, case_id, side, encoded, stored_hash, last_valid, pool, created_at),
        )
        return digest

    def _transport(self, accounts, *, slot=SLOT, tip=SLOT, priced=True, now=NOW):
        sol = _sol_price(now, slot)
        usdc = Path("tests/fixtures/pyth_usdc_usd_price_update_v2.bin").read_bytes()

        def transport(request):
            if request.get("method") == "getSlot":
                return {"status": 200, "body": {"result": tip}}
            if request.get("method") == "getMinimumBalanceForRentExemption":
                return {"status": 200, "body": {"result": solana_tx.TOKEN_ACCOUNT_RENT}}
            if request.get("method") == "getBlockHeight":
                return {"status": 200, "body": {"result": 90}}
            if request.get("method") == "isBlockhashValid":
                return {"status": 200, "body": {"result": {"value": True}}}
            if request.get("method") == "hermes_price":
                return {"status": 200, "body": {
                    "parsed": [{
                        "id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
                        "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": now},
                        "status": "trading",
                    }],
                }}
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

    def _message(self, values, source, dest, tip, *, side="buy", amount=4, quoted=100, slippage=50, blockhash=None):
        return _tx(
            _metas(source, dest, values), _route(quoted, slippage, True, amount=amount),
            limit=200_000, price=0, side=side, extra_transfer=TIP_ACCOUNT, tip_lamports=tip,
            blockhash=blockhash or solana_tx.b58encode(b"\x44" * 32),
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
        self.assertTrue(passed["ticket_limit_proven"])
        self.assertTrue(passed["allocation_proven"])
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
        mint = solana_tx.b58encode(b"\x73" * 32)
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

    def test_the_signer_repeats_ticket_and_allocation_and_keeps_a_reduce_only_exit(self):
        import json
        import threading
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        transport = self._transport(accounts)
        pyth_sponsored.read_accounts(transport, NOW)

        huge = self._message(values, solana_tx.NATIVE_MINT, mint, 0, amount=1_000_000_000)
        huge_digest = self._store(huge, values["pool"], "entry", "bypass-ticket")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'bypass-ticket'", ('{"reserved_usd": "1.00"}',))
        refused = desk_loop.mock_phantom_sign(huge_digest, auth, NOW, transport)
        self.assertFalse(refused["signed"], refused)
        self.assertEqual(refused["reason"], "ticket")
        self.assertFalse(refused["ticket_limit_proven"])
        self.assertFalse(refused["allocation_proven"])

        hold = solana_tx.b58encode(b"\x76" * 32)
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('deposit-hold', ?, 'exited', ?, ?, ?, 'live', 'server')",
            (hold, json.dumps({
                "reserved_usd": "95.00", "remaining_raw": 0, "deposit_refund_proven": False,
                "deposits": {"mint_ata": {"account": "ata"}}, "live_receipts": [{"signature": "held"}],
            }), NOW, NOW),
        )
        sized = self._message(values, solana_tx.NATIVE_MINT, mint, 0, amount=60_000_000)
        sized_digest = self._store(sized, values["pool"], "entry", "bypass-book")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'bypass-book'", ('{"reserved_usd": "1.00"}',))
        crowded = desk_loop.mock_phantom_sign(sized_digest, auth, NOW, transport)
        self.assertFalse(crowded["signed"], crowded)
        self.assertEqual(crowded["reason"], "allocation")

        db.execute("DELETE FROM memecoin_cases")
        fresh = solana_tx.b58encode(b"\x77" * 32)
        results = []

        def claim(number):
            results.append(memecoin_desk.claim_open_ticket("race-%s" % number, fresh, "8.00", NOW))

        threads = [threading.Thread(target=claim, args=(number,)) for number in range(13)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for item in results if item.get("ok") is True), 12)
        self.assertEqual(sum(1 for item in results if item.get("reason") == "allocation"), 1)

        db.init_db()
        memecoin_desk._ensure()
        kept = db.query("SELECT snapshot_json FROM memecoin_cases WHERE id LIKE 'race-%'")
        self.assertEqual(len(kept), 12)
        self.assertTrue(all(json.loads(item["snapshot_json"])["reserved_usd"] == "8.00" for item in kept))
        again = memecoin_desk.claim_open_ticket("race-again", fresh, "8.00", NOW)
        self.assertEqual(again["reason"], "allocation")

        case_mint = solana_tx.b58encode(b"\x73" * 32)
        exit_values, exit_accounts = _pool(case_mint, solana_tx.NATIVE_MINT)
        exit_message = self._message(exit_values, case_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=4)
        exit_digest = self._store(exit_message, exit_values["pool"], "exit", "full-exit")
        db.execute("UPDATE memecoin_cases SET stage = 'partial', snapshot_json = ? WHERE id = 'full-exit'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('full-book', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x78" * 32), '{"reserved_usd": "100.00"}', NOW, NOW),
        )
        pyth_sponsored.read_accounts(self._transport(exit_accounts), NOW)
        sold = desk_loop.mock_phantom_sign(exit_digest, auth, NOW, self._transport(exit_accounts))
        self.assertTrue(sold["signed"], sold)
        self.assertFalse(sold["ticket_limit_proven"])
        self.assertFalse(sold["allocation_proven"])
        gates = next(item for item in desk_loop.remaining_live_gates() if item["gate"] == "ticket_limit_proven")
        book = next(item for item in desk_loop.remaining_live_gates() if item["gate"] == "allocation_proven")
        self.assertTrue(gates["closed"])
        self.assertTrue(book["closed"])

    def test_tips_unknown_programs_and_a_zero_deposit_are_not_free(self):
        import base64
        import json
        from decimal import Decimal
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        exit_values, exit_accounts = _pool(mint, solana_tx.NATIVE_MINT)
        transport = self._transport(accounts)
        exit_transport = self._transport(exit_accounts)
        pyth_sponsored.read_accounts(transport, NOW)
        pyth_sponsored.read_accounts(exit_transport, NOW)
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('full-book', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x7a" * 32), '{"reserved_usd": "100.00"}', NOW, NOW),
        )
        exit_values, _exit_accounts = _pool(mint, solana_tx.NATIVE_MINT)
        tipped_exit = self._message(exit_values, mint, solana_tx.NATIVE_MINT, 2_000_000_000, side="exit", amount=4)
        exit_digest = self._store(tipped_exit, exit_values["pool"], "exit", "tip-exit")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'tip-exit'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        exit_refused = desk_loop.mock_phantom_sign(exit_digest, auth, NOW, exit_transport)
        self.assertFalse(exit_refused["signed"], exit_refused)
        self.assertEqual(exit_refused["reason"], "ticket")
        self.assertFalse(exit_refused["ticket_limit_proven"])
        self.assertFalse(exit_refused["allocation_proven"])

        tipped_entry = self._message(values, solana_tx.NATIVE_MINT, mint, 2_000_000_000, amount=4)
        entry_digest = self._store(tipped_entry, values["pool"], "entry", "tip-entry")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'tip-entry'", ('{"reserved_usd": "1.00"}',))
        entry_refused = desk_loop.mock_phantom_sign(entry_digest, auth, NOW, transport)
        self.assertFalse(entry_refused["signed"], entry_refused)
        self.assertEqual(entry_refused["reason"], "ticket")
        self.assertFalse(entry_refused["ticket_limit_proven"])

        plain = self._message(exit_values, mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=4)
        decoded = solana_tx.decode_transaction(base64.b64decode(plain))
        keys = list(decoded.static_keys) + [solana_tx.b58encode(b"\x99" * 32)]
        instructions = [(ix.program_index, list(ix.account_indexes), bytes(ix.data)) for ix in decoded.instructions]
        instructions.append((len(keys) - 1, [], b"\x01"))
        unknown = solana_tx.encode_legacy(decoded.fee_payer, keys, decoded.readonly_unsigned + 1, instructions, decoded.blockhash, required=decoded.required_signatures)
        unknown_digest = self._store(unknown, exit_values["pool"], "exit", "unknown-program")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'unknown-program'", ('{"reserved_usd": "0.00"}',))
        unknown_refused = desk_loop.mock_phantom_sign(unknown_digest, auth, NOW, transport)
        self.assertFalse(unknown_refused["signed"], unknown_refused)
        self.assertEqual(unknown_refused["reason"], "unparseable_instruction")
        self.assertFalse(unknown_refused["ticket_limit_proven"])

        db.execute("DELETE FROM memecoin_cases")
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('zero-deposit', ?, 'exited', ?, ?, ?, 'live', 'server')",
            (mint, json.dumps({
                "reserved_usd": "0.00", "remaining_raw": 0, "deposit_refund_proven": False,
                "deposits": {"mint_ata": {"account": "ata"}},
            }), NOW, NOW),
        )
        blank = db.exclusive(lambda conn: memecoin_desk.capital_room(conn, "new-ticket", memecoin_desk.TICKET, now=NOW))
        self.assertEqual(blank["reason"], "exposure_unknown", blank)
        db.execute(
            "UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'zero-deposit'",
            (json.dumps({
                "reserved_usd": "0.00", "remaining_raw": 0, "deposit_refund_proven": False,
                "deposits": {"mint_ata": {"account": "ata", "rent_lamports": 950_000_000}},
            }),),
        )
        priced = db.exclusive(lambda conn: memecoin_desk.capital_room(conn, "new-ticket", memecoin_desk.TICKET, now=NOW))
        self.assertEqual(priced["reason"], "allocation", priced)
        held = db.exclusive(lambda conn: memecoin_desk._held_conn(conn, unknown_is_none=True, now=NOW))
        self.assertGreater(held, Decimal("90"))

    def test_a_payer_token_transfer_and_an_uncapped_priority_fee_are_refused(self):
        import base64
        from tests.test_jupiter_route_v2 import _metas, _route, _tx
        case_mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, case_mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        exit_values, exit_accounts = _pool(case_mint, solana_tx.NATIVE_MINT)
        transport = self._transport(exit_accounts)
        pyth_sponsored.read_accounts(transport, NOW)
        wallet = phantom_transport.REQUIRED_WALLET

        def extend(encoded, extra_keys, program_index, ix_accounts, ix_data):
            decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
            keys = list(decoded.static_keys) + extra_keys
            instructions = [(ix.program_index, list(ix.account_indexes), bytes(ix.data)) for ix in decoded.instructions]
            instructions.append((program_index, ix_accounts, ix_data))
            return solana_tx.encode_legacy(
                decoded.fee_payer, keys, decoded.readonly_unsigned + len(extra_keys), instructions,
                decoded.blockhash, required=decoded.required_signatures,
            )

        plain = self._message(exit_values, case_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=4)
        decoded = solana_tx.decode_transaction(base64.b64decode(plain))
        token_program = decoded.static_keys.index(solana_tx.TOKEN_PROGRAM)
        source = solana_tx.b58encode(b"\xab" * 32)
        dest = solana_tx.b58encode(b"\xac" * 32)
        stolen = extend(
            plain, [source, dest], token_program,
            [len(decoded.static_keys), len(decoded.static_keys) + 1, 0],
            bytes([3]) + (2_000_000_000).to_bytes(8, "little"),
        )
        stolen_digest = self._store(stolen, exit_values["pool"], "exit", "foreign-transfer")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'foreign-transfer'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        moved = desk_loop.mock_phantom_sign(stolen_digest, auth, NOW, transport)
        self.assertFalse(moved["signed"], moved)
        self.assertEqual(moved["reason"], "unparseable_instruction")
        self.assertFalse(moved["ticket_limit_proven"])
        self.assertFalse(moved["allocation_proven"])

        position = solana_tx.associated_token_address(wallet, case_mint, solana_tx.TOKEN_PROGRAM)
        within = extend(
            plain, [position, dest], token_program,
            [len(decoded.static_keys), len(decoded.static_keys) + 1, 0],
            bytes([3]) + (4).to_bytes(8, "little"),
        )
        within_digest = self._store(within, exit_values["pool"], "exit", "position-transfer")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'position-transfer'", ('{"reserved_usd": "0.00", "remaining_raw": 8}',))
        kept = desk_loop.mock_phantom_sign(within_digest, auth, NOW, transport)
        self.assertTrue(kept["signed"], kept)
        self.assertFalse(kept["ticket_limit_proven"])
        self.assertFalse(kept["allocation_proven"])
        over = extend(
            plain, [position, dest], token_program,
            [len(decoded.static_keys), len(decoded.static_keys) + 1, 0],
            bytes([3]) + (5).to_bytes(8, "little"),
        )
        over_digest = self._store(over, exit_values["pool"], "exit", "position-over")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'position-over'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        too_much = desk_loop.mock_phantom_sign(over_digest, auth, NOW, transport)
        self.assertFalse(too_much["signed"], too_much)
        self.assertEqual(too_much["reason"], "unparseable_instruction")

        expensive = _tx(
            _metas(case_mint, solana_tx.NATIVE_MINT, exit_values), _route(100, 50, True, amount=4),
            limit=200_000, price=10**12, side="exit", extra_transfer=None, tip_lamports=0,
            blockhash=solana_tx.b58encode(b"\x44" * 32),
        )
        expensive_digest = self._store(expensive, exit_values["pool"], "exit", "priority-cap")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'priority-cap'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        capped = desk_loop.mock_phantom_sign(expensive_digest, auth, NOW, transport)
        self.assertFalse(capped["signed"], capped)
        self.assertEqual(capped["reason"], "fee_cap")
        self.assertFalse(capped["ticket_limit_proven"])
        self.assertFalse(capped["allocation_proven"])

        duplicated = extend(plain, [], decoded.static_keys.index(solana_tx.COMPUTE_BUDGET), [], bytes([2]) + (200_000).to_bytes(4, "little"))
        duplicated_digest = self._store(duplicated, exit_values["pool"], "exit", "duplicate-budget")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'duplicate-budget'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        twice = desk_loop.mock_phantom_sign(duplicated_digest, auth, NOW, transport)
        self.assertFalse(twice["signed"], twice)
        self.assertEqual(twice["reason"], "unparseable_instruction")

    def test_route_and_tip_add_and_an_oversized_exit_route_is_refused(self):
        from tests.test_jupiter_route_v2 import _metas, _route, _tx
        case_mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, case_mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        transport = self._transport(accounts)
        pyth_sponsored.read_accounts(transport, NOW)
        blockhash = solana_tx.b58encode(b"\x44" * 32)

        both = _tx(
            _metas(solana_tx.NATIVE_MINT, case_mint, values), _route(100, 50, True, amount=50_000_000),
            limit=200_000, price=1_000, side="exit", extra_transfer=solana_tx.b58encode(b"\x91" * 32),
            tip_lamports=50_000_000, blockhash=blockhash,
        )
        both_digest = self._store(both, values["pool"], "entry", "route-plus-tip")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'route-plus-tip'", ('{"reserved_usd": "1.00"}',))
        summed = desk_loop.mock_phantom_sign(both_digest, auth, NOW, transport)
        self.assertFalse(summed["signed"], summed)
        self.assertEqual(summed["reason"], "ticket")
        self.assertFalse(summed["ticket_limit_proven"])

        once = self._message(values, solana_tx.NATIVE_MINT, case_mint, 0, amount=50_000_000)
        once_digest = self._store(once, values["pool"], "entry", "wrap-once")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'wrap-once'", ('{"reserved_usd": "1.00"}',))
        counted = desk_loop.mock_phantom_sign(once_digest, auth, NOW, transport)
        self.assertTrue(counted["signed"], counted)
        self.assertTrue(counted["ticket_limit_proven"])
        self.assertTrue(counted["allocation_proven"])

        exit_values, exit_accounts = _pool(case_mint, solana_tx.NATIVE_MINT)
        oversized = self._message(exit_values, case_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=1_000_000)
        over_digest = self._store(oversized, exit_values["pool"], "exit", "route-over-position")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'route-over-position'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        over_position = desk_loop.mock_phantom_sign(over_digest, auth, NOW, self._transport({**accounts, **exit_accounts}))
        self.assertFalse(over_position["signed"], over_position)
        self.assertEqual(over_position["reason"], "unparseable_instruction")
        self.assertFalse(over_position["ticket_limit_proven"])

        foreign_mint = solana_tx.b58encode(b"\x44" * 32)
        foreign_values, foreign_accounts = _pool(foreign_mint, solana_tx.NATIVE_MINT)
        foreign = self._message(foreign_values, foreign_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=4)
        foreign_digest = self._store(foreign, foreign_values["pool"], "exit", "foreign-source")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'foreign-source'", ('{"reserved_usd": "0.00", "remaining_raw": 4}',))
        foreign_source = desk_loop.mock_phantom_sign(foreign_digest, auth, NOW, self._transport({**accounts, **foreign_accounts}))
        self.assertFalse(foreign_source["signed"], foreign_source)
        self.assertEqual(foreign_source["reason"], "unparseable_instruction")
        self.assertFalse(foreign_source["allocation_proven"])

        import base64
        import solana_jupiter
        buy = self._message(values, solana_tx.NATIVE_MINT, case_mint, 0, amount=2_000_000_000)
        decoded = solana_tx.decode_transaction(base64.b64decode(buy))
        position = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, case_mint, solana_tx.TOKEN_PROGRAM)
        wsol = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)

        def rewrite(slot, pubkey=None, flip_direction=False):
            keys = list(decoded.static_keys)
            extra = 0
            if pubkey is not None and pubkey not in keys:
                keys.append(pubkey)
                key_index = len(keys) - 1
                extra = 1
            elif pubkey is not None:
                key_index = keys.index(pubkey)
            else:
                key_index = None
            instructions = []
            for ix in decoded.instructions:
                accounts_ix = list(ix.account_indexes)
                data = bytearray(ix.data)
                if decoded.static_keys[ix.program_index] == solana_jupiter.PROGRAM_ID:
                    if key_index is not None:
                        accounts_ix[slot] = key_index
                    if flip_direction:
                        data[35] = 0 if data[35] else 1
                instructions.append((ix.program_index, accounts_ix, bytes(data)))
            return solana_tx.encode_legacy(
                decoded.fee_payer, keys, decoded.readonly_unsigned + extra, instructions,
                decoded.blockhash, required=decoded.required_signatures,
            )

        hop_input = None
        hop_output = None
        for ix in decoded.instructions:
            if decoded.static_keys[ix.program_index] != solana_jupiter.PROGRAM_ID:
                continue
            for offset, index in enumerate(ix.account_indexes):
                if decoded.static_keys[index] == solana_whirlpool.PROGRAM_ID:
                    hop_input = offset + 4
                    hop_output = offset + 6
        self.assertIsNotNone(hop_input)
        route_ix = next(ix for ix in decoded.instructions if decoded.static_keys[ix.program_index] == solana_jupiter.PROGRAM_ID)
        self.assertEqual(decoded.static_keys[route_ix.account_indexes[hop_input]], wsol)
        self.assertEqual(decoded.static_keys[route_ix.account_indexes[1]], wsol)
        self.assertEqual(decoded.static_keys[route_ix.account_indexes[hop_output]], position)

        def refuse(encoded, name):
            digest = self._store(encoded, values["pool"], "entry", name)
            db.execute(
                "UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?",
                ('{"reserved_usd": "1.00", "remaining_raw": 2000000000}', name),
            )
            result = desk_loop.mock_phantom_sign(digest, auth, NOW, transport)
            self.assertFalse(result["signed"], result)
            self.assertEqual(result["reason"], "unparseable_instruction", result)
            self.assertFalse(result["ticket_limit_proven"])
            self.assertFalse(result["allocation_proven"])

        refuse(rewrite(1, position), "decoy-index")
        refuse(rewrite(hop_input, position), "decoy-hop")
        refuse(rewrite(hop_output, wsol), "wrong-output")
        refuse(rewrite(None, flip_direction=True), "wrong-direction")

    def test_the_signer_requires_a_fresh_pyth_price_and_an_unexpired_quote(self):
        import copy
        import json
        from pathlib import Path
        case_mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, case_mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        base = self._transport(accounts)
        template = json.loads((Path(__file__).resolve().parent / "fixtures" / "hermes_sol_usd.json").read_text(encoding="utf-8"))

        def feed(publish_time, *, conf="1", status="trading", present=True):
            body = copy.deepcopy(template)
            if not present:
                body["parsed"] = []
            else:
                body["parsed"][0]["status"] = status
                body["parsed"][0]["price"]["publish_time"] = publish_time
                body["parsed"][0]["price"]["conf"] = conf
            return body

        def transport_for(payload, *, height=90, valid=True):
            def transport(request):
                if request.get("method") == "hermes_price":
                    return {"status": 200, "body": payload}
                if request.get("method") == "getBlockHeight":
                    if height is None:
                        return {"status": 200, "body": {"result": None}}
                    return {"status": 200, "body": {"result": height}}
                if request.get("method") == "isBlockhashValid":
                    if valid is None:
                        return {"status": 200, "body": {"result": {}}}
                    return {"status": 200, "body": {"result": {"value": valid}}}
                return base(request)
            return transport

        def sign(name, payload, *, amount, when=NOW, height=90, valid=True, created_at=NOW, last_valid=100):
            message = self._message(values, solana_tx.NATIVE_MINT, case_mint, 0, amount=amount)
            digest = self._store(message, values["pool"], "entry", name, created_at=created_at, last_valid=last_valid)
            return desk_loop.mock_phantom_sign(digest, auth, when, transport_for(payload, height=height, valid=valid))

        happy = sign("fresh-sign", feed(NOW), amount=4)
        self.assertTrue(happy["signed"], happy)
        self.assertTrue(happy["price_freshness_proven"])
        self.assertTrue(happy["ticket_limit_proven"])
        self.assertTrue(happy["allocation_proven"])
        self.assertFalse(happy["live"])
        boundary = sign("fresh-boundary", feed(NOW - 30), amount=5)
        self.assertTrue(boundary["signed"], boundary)
        self.assertTrue(boundary["price_freshness_proven"])
        stale = sign("fresh-stale", feed(NOW - 31), amount=6)
        self.assertFalse(stale["signed"], stale)
        self.assertEqual(stale["reason"], "price_stale")
        self.assertFalse(stale["price_freshness_proven"])
        future = sign("fresh-future", feed(NOW + 6), amount=7)
        self.assertEqual(future["reason"], "price_stale")
        self.assertFalse(future["price_freshness_proven"])
        wide = sign("fresh-wide", feed(NOW, conf="2"), amount=8)
        self.assertEqual(wide["reason"], "price_conf")
        self.assertFalse(wide["ticket_limit_proven"])
        missing = sign("fresh-missing", feed(NOW, present=False), amount=9)
        self.assertEqual(missing["reason"], "price_unknown")
        halted = sign("fresh-halted", feed(NOW, status="halted"), amount=10)
        self.assertEqual(halted["reason"], "price_unknown")
        self.assertFalse(halted["price_freshness_proven"])
        expired = sign("fresh-quote", feed(NOW), amount=11, created_at=NOW - 21)
        self.assertEqual(expired["reason"], "quote_expired")
        self.assertFalse(expired["price_freshness_proven"])
        at_last = sign("fresh-height", feed(NOW), amount=12, height=100)
        self.assertEqual(at_last["reason"], "blockhash_expired")
        self.assertFalse(at_last["allocation_proven"])
        unknown_head = sign("fresh-head", feed(NOW), amount=13, height=None)
        self.assertEqual(unknown_head["reason"], "blockhash_unknown")
        self.assertFalse(unknown_head["price_freshness_proven"])
        self.assertFalse(unknown_head["ticket_limit_proven"])

        exit_values, exit_accounts = _pool(case_mint, solana_tx.NATIVE_MINT)
        exit_base = self._transport(exit_accounts)

        def exit_transport(payload, *, height=90):
            def transport(request):
                if request.get("method") == "hermes_price":
                    return {"status": 200, "body": payload}
                if request.get("method") == "getBlockHeight":
                    return {"status": 200, "body": {"result": height}}
                return exit_base(request)
            return transport

        exit_message = self._message(exit_values, case_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=4)
        exit_digest = self._store(exit_message, exit_values["pool"], "exit", "fresh-exit")
        db.execute(
            "UPDATE memecoin_cases SET stage = 'partial', snapshot_json = ? WHERE id = 'fresh-exit'",
            ('{"reserved_usd": "0.00", "remaining_raw": 4}',),
        )
        reduced = desk_loop.mock_phantom_sign(exit_digest, auth, NOW, exit_transport(feed(NOW)))
        self.assertTrue(reduced["signed"], reduced)
        self.assertTrue(reduced["price_freshness_proven"])
        self.assertFalse(reduced["ticket_limit_proven"])
        self.assertFalse(reduced["allocation_proven"])
        stale_exit = self._message(exit_values, case_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=5)
        stale_digest = self._store(stale_exit, exit_values["pool"], "exit", "stale-exit")
        db.execute(
            "UPDATE memecoin_cases SET stage = 'partial', snapshot_json = ? WHERE id = 'stale-exit'",
            ('{"reserved_usd": "0.00", "remaining_raw": 5}',),
        )
        refused_exit = desk_loop.mock_phantom_sign(stale_digest, auth, NOW, exit_transport(feed(NOW - 31)))
        self.assertFalse(refused_exit["signed"], refused_exit)
        self.assertEqual(refused_exit["reason"], "price_stale")
        self.assertFalse(refused_exit["price_freshness_proven"])

    def test_a_non_finite_clock_or_a_different_message_blockhash_cannot_sign(self):
        import copy
        import json
        from pathlib import Path
        case_mint = solana_tx.b58encode(b"\x73" * 32)
        stored_hash = solana_tx.b58encode(b"\x44" * 32)
        message_hash = solana_tx.b58encode(b"\x99" * 32)
        exit_values, exit_accounts = _pool(case_mint, solana_tx.NATIVE_MINT)
        base = self._transport(exit_accounts)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        template = json.loads((Path(__file__).resolve().parent / "fixtures" / "hermes_sol_usd.json").read_text(encoding="utf-8"))

        def feed(publish_time, *, price="100", conf="0", expo=0):
            body = copy.deepcopy(template)
            slot = body["parsed"][0]["price"]
            slot["publish_time"] = publish_time
            slot["price"] = price
            slot["conf"] = conf
            slot["expo"] = expo
            body["parsed"][0]["status"] = "trading"
            return body

        def sign(name, amount, payload, when, *, created_at=1, message_blockhash=stored_hash, stored_blockhash=stored_hash, height=90):
            asked = []
            message = self._message(
                exit_values, case_mint, solana_tx.NATIVE_MINT, 0, side="exit", amount=amount, blockhash=message_blockhash,
            )
            digest = self._store(
                message, exit_values["pool"], "exit", name, created_at=created_at, blockhash=stored_blockhash,
            )
            db.execute(
                "UPDATE memecoin_cases SET stage = 'partial', snapshot_json = ? WHERE id = ?",
                ('{"reserved_usd": "0.00", "remaining_raw": %s}' % amount, name),
            )

            def transport(request):
                if request.get("method") == "hermes_price":
                    return {"status": 200, "body": payload}
                if request.get("method") == "getBlockHeight":
                    return {"status": 200, "body": {"result": height}}
                if request.get("method") == "isBlockhashValid":
                    asked.append(request.get("blockhash"))
                    return {"status": 200, "body": {"result": {"value": True}}}
                return base(request)

            signed = desk_loop.mock_phantom_sign(digest, auth, when, transport)
            signed["asked"] = asked
            return signed

        for amount, bad in enumerate((float("nan"), float("inf"), float("-inf")), start=4):
            refused = sign("clock-%s" % amount, amount, feed(1), bad, created_at=1)
            self.assertFalse(refused["signed"], refused)
            self.assertEqual(refused["reason"], "price_unknown")
            self.assertFalse(refused["price_freshness_proven"])
            self.assertFalse(refused["ticket_limit_proven"])
            self.assertFalse(refused["allocation_proven"])
        stopped = sign("clock-zero", 8, feed(0), 0, created_at=0)
        self.assertFalse(stopped["signed"], stopped)
        self.assertEqual(stopped["reason"], "price_unknown")
        self.assertFalse(stopped["price_freshness_proven"])
        for amount, field in enumerate(("publish_time", "price", "conf", "expo"), start=9):
            value = float("nan")
            if field == "publish_time":
                payload = feed(value)
            elif field == "price":
                payload = feed(NOW, price=value)
            elif field == "conf":
                payload = feed(NOW, conf=value)
            else:
                payload = feed(NOW, expo=value)
            parsed = pyth_sponsored.assess_signer_price(payload, NOW)
            self.assertEqual(parsed["reason"], "price_unknown", field)
            self.assertIsNot(parsed.get("ok"), True)
            refused = sign("nan-%s" % field, amount, payload, NOW, created_at=NOW)
            self.assertFalse(refused["signed"], refused)
            self.assertEqual(refused["reason"], "price_unknown", field)
            self.assertFalse(refused["price_freshness_proven"])

        class _Message:
            def __init__(self, blockhash):
                self.blockhash = blockhash

        nan_quote = desk_loop._signer_quote_and_blockhash(
            {"created_at": float("nan"), "blockhash": stored_hash, "last_valid_block_height": 100},
            _Message(stored_hash), base, NOW,
        )
        self.assertIsNot(nan_quote.get("ok"), True)
        self.assertEqual(nan_quote["reason"], "quote_expired")
        inf_quote = desk_loop._signer_quote_and_blockhash(
            {"created_at": float("inf"), "blockhash": stored_hash, "last_valid_block_height": 100},
            _Message(stored_hash), base, NOW,
        )
        self.assertEqual(inf_quote["reason"], "quote_expired")

        mismatch = sign("hash-mismatch", 14, feed(NOW), NOW, created_at=NOW, message_blockhash=message_hash, stored_blockhash=stored_hash)
        self.assertFalse(mismatch["signed"], mismatch)
        self.assertEqual(mismatch["reason"], "blockhash_mismatch")
        self.assertEqual(mismatch["asked"], [message_hash])
        self.assertNotIn(stored_hash, mismatch["asked"])
        self.assertFalse(mismatch["price_freshness_proven"])
        self.assertFalse(mismatch["ticket_limit_proven"])
        missing = sign("hash-missing", 15, feed(NOW), NOW, created_at=NOW, stored_blockhash="")
        self.assertFalse(missing["signed"], missing)
        self.assertEqual(missing["reason"], "blockhash_unknown")
        self.assertEqual(missing["asked"], [])
        self.assertFalse(missing["price_freshness_proven"])
        bound = sign("hash-bound", 16, feed(NOW), NOW, created_at=NOW)
        self.assertTrue(bound["signed"], bound)
        self.assertEqual(bound["asked"], [stored_hash])
        self.assertTrue(bound["price_freshness_proven"])
        self.assertFalse(bound["ticket_limit_proven"])
        self.assertFalse(bound["rent_bound_proven"])

    def test_an_unparseable_hermes_number_is_price_unknown(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        message = self._message(values, solana_tx.NATIVE_MINT, mint, 0, amount=4)
        digest = self._store(message, values["pool"], "entry", "overflow-price")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = 'overflow-price'", ('{"reserved_usd": "1.00"}',))
        base = self._transport(accounts)

        def sign(payload):
            def transport(request):
                if request.get("method") == "hermes_price":
                    return {"status": 200, "body": payload}
                return base(request)
            return desk_loop.mock_phantom_sign(digest, auth, NOW, transport)

        huge = {"parsed": [{"status": "trading", "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": 10**1000}}]}
        dashed = {"parsed": [{"status": "trading", "price": {"price": "--5", "conf": "0", "expo": 0, "publish_time": NOW}}]}
        huge_price = {"parsed": [{"status": "trading", "price": {"price": 10**1000, "conf": "0", "expo": 0, "publish_time": NOW}}]}
        for payload in (huge, dashed, huge_price):
            refused = sign(payload)
            self.assertFalse(refused["signed"], refused)
            self.assertEqual(refused["reason"], "price_unknown")
            self.assertFalse(refused["price_freshness_proven"])
            self.assertFalse(refused["ticket_limit_proven"])
            self.assertFalse(refused["allocation_proven"])
            self.assertFalse(refused["rent_bound_proven"])
            self.assertFalse(refused["live"])

    def test_the_signer_bounds_rent_at_the_fixture_minimum(self):
        mint = solana_tx.b58encode(b"\x73" * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        auth = {"ok": True, "mock": True, "network_called": False, "wallet_id": phantom_transport.REQUIRED_WALLET_ID, "wallet": phantom_transport.REQUIRED_WALLET}
        base = self._transport(accounts)
        wallet = phantom_transport.REQUIRED_WALLET
        position = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
        rent = solana_tx.TOKEN_ACCOUNT_RENT

        def system_create(encoded, account, lamports, space):
            decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
            keys = list(decoded.static_keys)
            readonly = decoded.readonly_unsigned
            if account not in keys:
                keys.append(account)
                readonly += 1
            data = (0).to_bytes(4, "little") + int(lamports).to_bytes(8, "little") + int(space).to_bytes(8, "little") + solana_tx.b58decode(solana_tx.TOKEN_PROGRAM)
            instructions = [(ix.program_index, list(ix.account_indexes), bytes(ix.data)) for ix in decoded.instructions]
            instructions.append((keys.index(solana_tx.SYSTEM_PROGRAM), [0, keys.index(account)], data))
            return solana_tx.encode_legacy(decoded.fee_payer, keys, readonly, instructions, decoded.blockhash, required=decoded.required_signatures)

        def duplicate_ata(encoded):
            decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
            keys = list(decoded.static_keys)
            instructions = [(ix.program_index, list(ix.account_indexes), bytes(ix.data)) for ix in decoded.instructions]
            create = next(ix for ix in instructions if keys[ix[0]] == solana_tx.ASSOCIATED_TOKEN and ix[2] == bytes([1]))
            instructions.append(create)
            return solana_tx.encode_legacy(decoded.fee_payer, keys, decoded.readonly_unsigned, instructions, decoded.blockhash, required=decoded.required_signatures)

        def unknown_program(encoded):
            decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
            keys = list(decoded.static_keys) + [solana_tx.b58encode(b"\x99" * 32)]
            instructions = [(ix.program_index, list(ix.account_indexes), bytes(ix.data)) for ix in decoded.instructions]
            instructions.append((len(keys) - 1, [], b"\x01"))
            return solana_tx.encode_legacy(decoded.fee_payer, keys, decoded.readonly_unsigned + 1, instructions, decoded.blockhash, required=decoded.required_signatures)

        def sign(name, encoded, transport):
            digest = self._store(encoded, values["pool"], "entry", name)
            db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", ('{"reserved_usd": "1.00"}', name))
            return desk_loop.mock_phantom_sign(digest, auth, NOW, transport)

        def without_rent(request):
            if request.get("method") == "getMinimumBalanceForRentExemption":
                return {"status": 200, "body": {"result": None}}
            return base(request)

        happy_message = self._message(values, solana_tx.NATIVE_MINT, mint, 0, amount=4)
        happy = sign("rent-happy", happy_message, base)
        self.assertTrue(happy["signed"], happy)
        self.assertTrue(happy["rent_bound_proven"])
        self.assertTrue(happy["price_freshness_proven"])
        self.assertTrue(happy["ticket_limit_proven"])
        self.assertTrue(happy["allocation_proven"])
        self.assertFalse(happy["live"])
        self.assertIsNot(happy.get("execution_eligible"), True)
        self.assertIsNot(happy.get("preflight_ready"), True)
        self.assertIsNot(happy.get("gate_complete"), True)
        self.assertIsNot(happy.get("live_ready"), True)

        def refused(name, encoded, transport, reason):
            result = sign(name, encoded, transport)
            self.assertFalse(result["signed"], result)
            self.assertEqual(result["reason"], reason, result)
            self.assertFalse(result["rent_bound_proven"])
            self.assertFalse(result["price_freshness_proven"])
            self.assertFalse(result["ticket_limit_proven"])
            self.assertFalse(result["allocation_proven"])
            return result

        refused("rent-space", system_create(happy_message, position, rent, 166), base, "rent_exceeds")
        refused("rent-extra", system_create(happy_message, solana_tx.b58encode(b"\x94" * 32), rent, 165), base, "rent_unknown")
        refused("rent-duplicate", duplicate_ata(happy_message), base, "rent_unknown")
        refused("rent-lamports", system_create(happy_message, position, rent + 1, 165), base, "rent_exceeds")
        refused("rent-program", unknown_program(happy_message), base, "unparseable_instruction")
        other = self._message(values, solana_tx.NATIVE_MINT, mint, 0, amount=5)
        refused("rent-table", other, without_rent, "rent_unknown")
