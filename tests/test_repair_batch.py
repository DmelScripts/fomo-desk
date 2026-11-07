"""Exploit regressions for the offline repair. Fake reads and a temp database only."""
import base64
import hashlib
import json
import time
import unittest

import db
import execution_transport
import exit_policy
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_read_rpc
import solana_tx
import solana_whirlpool
import spend_policy
import usdc_valuation
from tests.support import temp_desk_env
from tests.whirlpool_fixture import FakeTransport, reader

WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID


def _quote(**overrides):
    body = {
        "price": 99_990_000, "conf": 10_000, "expo": -8,
        "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
    }
    body.update(overrides)
    return body


def _header(raw: bytes):
    count, index = solana_tx._compact(raw, 0)
    message = raw[index + 64 * count:]
    return message[0], message[2]


def _reencode(tx_b64: str, instructions, readonly_unsigned=None, keys=None):
    raw = base64.b64decode(tx_b64)
    required, default_readonly = _header(raw)
    decoded = solana_tx.decode_transaction(raw)
    return solana_tx.encode_legacy(
        decoded.fee_payer,
        decoded.static_keys if keys is None else keys,
        default_readonly if readonly_unsigned is None else readonly_unsigned,
        instructions,
        decoded.blockhash,
        required=required,
    )


def _instructions(tx_b64: str):
    decoded = solana_tx.decode_transaction(base64.b64decode(tx_b64))
    return decoded, [(ix.program_index, list(ix.account_indexes), ix.data) for ix in decoded.instructions]


class RepairBatchTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        exit_policy.set_approval(True)
        usdc_valuation.enable()
        usdc_valuation.set_quote(_quote())
        self.addCleanup(lambda: usdc_valuation.set_quote(None))
        solana_read_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))

    def test_injected_quote_is_not_a_live_provider(self):
        body = usdc_valuation.current()
        self.assertFalse(body["live_provider"])
        self.assertFalse(body["fetched"])
        self.assertEqual(body["origin"], "injected_quote")
        self.assertIsNone(body["accepted_band"])

    def test_upper_bound_above_par_rounds_the_ticket_down_to_eight(self):
        usdc_valuation.set_quote(_quote(price=100_000_000, conf=5_000_000))
        built, _entry, _exit, _rpc = execution_transport._assemble_read(466547240, reader())
        self.assertTrue(built["ok"], built.get("reason"))
        self.assertLess(built["entry_lamports"], 88_979_314)
        self.assertEqual(built["ticket_usd_ceiling"], "8.00")
        self.assertLessEqual(float(built["fee_usd"]), 0.05)
        self.assertLessEqual(float(built["exposure_usd_ceiling"]), 100.00)

    def test_extra_sol_transfer_duplicate_and_role_changes_do_not_spawn(self):
        built, entry, _exit, _rpc = execution_transport._assemble_read(466547240, reader())
        stored = {"entry_lamports": built["entry_lamports"], "entry_token_min_out": built["quote_token_min_out"]}
        decoded, instructions = _instructions(entry)
        raw = base64.b64decode(entry)
        required, readonly_unsigned = _header(raw)
        readonly_start = len(decoded.static_keys) - readonly_unsigned
        stranger = solana_tx.b58encode(bytes([7]) * 32)
        keys = decoded.static_keys[:readonly_start] + [stranger] + decoded.static_keys[readonly_start:]

        def remap(index):
            return index + 1 if index >= readonly_start else index

        moved = [(remap(program), [remap(item) for item in accounts], data) for program, accounts, data in instructions]
        system = keys.index(solana_tx.SYSTEM_PROGRAM)
        moved.append((system, [0, keys.index(stranger)], (2).to_bytes(4, "little") + (1_000_000_000).to_bytes(8, "little")))
        appended = solana_tx.encode_legacy(decoded.fee_payer, keys, readonly_unsigned, moved, decoded.blockhash, required=required)
        self.assertEqual(solana_direct_swap.prove_entry_tx(appended, stored, None), "transaction_shape")
        self._plant("extra", appended, stored)
        calls = []
        refused = execution_transport.send_after_identity("extra", appended, lambda *args: calls.append(args), binary="phantom")
        self.assertEqual(calls, [])
        self.assertEqual(refused["reason"], "transaction_shape")

        bundle = solana_read_rpc.load_bundle(reader())
        wsol = solana_tx.associated_token_address(WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
        usdc = solana_tx.associated_token_address(WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
        duplicate = solana_direct_swap.transaction_from_fields(
            solana_direct_swap.SOL_USDC_POOL, bundle["pool_data"], wsol, usdc,
            built["entry_lamports"], built["quote_token_min_out"], solana_direct_swap.MIN_SQRT_PRICE + 1,
            True, True, built["blockhash"], copies=2, wrap=True, transfer_lamports=built["entry_lamports"],
        )
        self.assertEqual(solana_direct_swap.prove_entry_tx(duplicate, stored, None), "transaction_shape")

        _decoded, base = _instructions(entry)
        swapped = []
        for program, accounts, data in base:
            if _decoded.static_keys[program] == solana_whirlpool.PROGRAM_ID:
                changed = list(accounts)
                changed[1] = changed[2]
                swapped.append((program, changed, data))
            else:
                swapped.append((program, accounts, data))
        bad_authority = _reencode(entry, swapped)
        self.assertEqual(solana_direct_swap.prove_entry_tx(bad_authority, stored, None), "authority")

        bad_oracle_ix = []
        for program, accounts, data in base:
            if _decoded.static_keys[program] == solana_whirlpool.PROGRAM_ID:
                changed = list(accounts)
                changed[10] = changed[0]
                bad_oracle_ix.append((program, changed, data))
            else:
                bad_oracle_ix.append((program, accounts, data))
        self.assertEqual(solana_direct_swap.prove_entry_tx(_reencode(entry, bad_oracle_ix), stored, None), "oracle")

        vault_keys = list(_decoded.static_keys)
        vault_index = next(accounts for program, accounts, _data in base if _decoded.static_keys[program] == solana_whirlpool.PROGRAM_ID)[4]
        vault_keys[vault_index] = stranger
        solana_read_rpc.enable_reads()
        solana_read_rpc.TRANSPORT = FakeTransport()
        self.assertEqual(
            solana_direct_swap.prove_entry_tx(_reencode(entry, base, keys=vault_keys), stored, execution_transport._spawn_accounts()["accounts"]),
            "whirlpool_vault_unbound",
        )
        privileged = _reencode(entry, base, readonly_unsigned=readonly_unsigned - 1)
        self.assertEqual(solana_direct_swap.prove_entry_tx(privileged, stored, None), "privileges")

    def test_missing_exit_proceeds_and_a_nine_million_fee_stay_held(self):
        planted = self._confirmed_exit()
        transport = FakeTransport()
        transport.statuses = {"context": {"slot": planted["slot"]}, "value": [{
            "slot": planted["slot"], "err": None, "confirmationStatus": "finalized",
        }]}
        transport.transaction = {
            "slot": planted["slot"],
            "meta": {
                "err": None, "fee": 9_000_000,
                "preTokenBalances": planted["pre_tokens"],
                "postTokenBalances": planted["post_tokens"],
            },
            "transaction": [planted["encoded"], "base64"],
        }
        solana_read_rpc.enable_reads()
        solana_read_rpc.TRANSPORT = transport
        booked = execution_transport.book_exit_close("exit", planted["signature"], 21)
        self.assertFalse(booked["booked"])
        self.assertEqual(booked["reason"], "proceeds_missing")
        row = db.query("SELECT status, reserved_usd, realized_pnl FROM phantom_reservations WHERE intent_id = 'exit'")[0]
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["reserved_usd"], "8.14")
        self.assertIsNone(row["realized_pnl"])

    def test_a_proven_close_books_once_and_survives_restart(self):
        planted = self._confirmed_exit()
        pre, post = planted["native"]
        transport = FakeTransport()
        transport.statuses = {"context": {"slot": planted["slot"]}, "value": [{
            "slot": planted["slot"], "err": None, "confirmationStatus": "finalized",
        }]}
        transport.transaction = {
            "slot": planted["slot"],
            "meta": {
                "err": None, "fee": planted["fee"],
                "preBalances": pre, "postBalances": post,
                "preTokenBalances": planted["pre_tokens"],
                "postTokenBalances": planted["post_tokens"],
            },
            "transaction": [planted["encoded"], "base64"],
        }
        solana_read_rpc.enable_reads()
        solana_read_rpc.TRANSPORT = transport
        usdc_valuation.set_quote(_quote(price=90_000_000, conf=10_000))
        booked = execution_transport.book_exit_close("exit", planted["signature"], 21)
        self.assertTrue(booked["booked"], booked)
        self.assertEqual(booked["realized_pnl"], planted["expected_pnl"])
        self.assertNotEqual(booked["realized_pnl"], "-0.10")
        again = execution_transport.book_exit_close("exit", planted["signature"], 21)
        self.assertFalse(again["booked"])
        db.init_db()
        kept = db.query("SELECT status, reserved_usd, realized_pnl FROM phantom_reservations WHERE intent_id = 'exit'")[0]
        self.assertEqual(kept["status"], "sim_closed")
        self.assertEqual(kept["realized_pnl"], planted["expected_pnl"])

    def test_direct_risk_rules_fire_and_the_loss_stop_does_not_block_the_exit(self):
        entry = 100_000_000
        opened = time.time()
        stored = {"entry_price_micro": entry, "opened_at": opened, "peak_price_micro": entry}
        self.assertEqual(execution_transport.assess_direct_exit(stored, entry * 106 // 100, opened)["action"], "stop")
        armed = execution_transport.assess_direct_exit(stored, entry * 80 // 100, opened)
        self.assertEqual(armed["action"], "hold")
        self.assertTrue(armed["trail_armed"])
        peaked = dict(stored, peak_price_micro=armed["peak_price_micro"], trail_armed=True)
        self.assertEqual(execution_transport.assess_direct_exit(peaked, armed["peak_price_micro"] * 112 // 100, opened)["action"], "trail")
        self.assertEqual(execution_transport.assess_direct_exit(stored, entry, opened + 3 * 3600)["action"], "timeout")
        self.assertEqual(execution_transport.assess_direct_exit(stored, entry * 95 // 100, opened + 3 * 3600)["action"], "hold")
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at, realized_pnl
            ) VALUES ('loss', 'sim_closed', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '0.00', 1, NULL, NULL, '89', ?, NULL, ?, ?, '-20.00')
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        planted = self._confirmed_exit()
        solana_read_rpc.enable_reads()

        def identity(argv, _env, _timeout):
            if argv[1:3] == ["wallet", "addresses"]:
                return 0, json.dumps({
                    "walletId": WALLET_ID, "organizationId": "org",
                    "addresses": [{"addressType": "solana", "address": WALLET}],
                })
            raise AssertionError("exit prepare must not send")

        mark = planted["entry_price"] * 106 // 100 + 1
        planned = execution_transport.plan_acquired_exit("exit", reader(), mark, identity)
        self.assertTrue(planned["ok"], planned)
        self.assertFalse(planned["loss_stop_blocks_exit"])
        self.assertFalse(planned["send"])
        self.assertFalse(planned["mounted"])
        self.assertEqual(planned["action"], "stop")

    def _plant(self, intent_id, tx_b64, stored):
        message = hashlib.sha256(solana_tx.message_bytes(tx_b64)).hexdigest()
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES (?, 'sim_open', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '8.14', 1, NULL, NULL, '89', ?, ?, ?, ?)
            """,
            (intent_id, WALLET_ID, WALLET, now, json.dumps({
                "origin": "direct_whirlpool", "message_sha256": message, **stored,
            }), now, now),
        )
        execution_transport.remember_review(intent_id, tx_b64)

    def _confirmed_exit(self):
        built, _entry, _exit, _rpc = execution_transport._assemble_read(466547240, reader())
        acquired = built["quote_token_min_out"] + 100
        bundle = solana_read_rpc.load_bundle(reader())
        tx, quote = solana_direct_swap.build_exit(
            bundle["pool"], bundle["pool_data"], bundle["accounts"], 100, bundle["blockhash"], bundle["context_slot"],
            acquired, built["wallet_usdc_raw"], minimum=built["quote_token_min_out"],
            estimated=built["quote_token_est_out"], ata_balance=built["wallet_usdc_raw"],
        )
        raw = base64.b64decode(tx)
        signed = bytes([raw[0]]) + (b"\x44" * 64) + raw[1 + 64:]
        encoded = base64.b64encode(signed).decode("ascii")
        decoded = solana_tx.decode_transaction(signed)
        usdc = solana_tx.associated_token_address(WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
        index = decoded.static_keys.index(usdc)
        pre_usdc = built["wallet_usdc_raw"]
        fee = built["network_fee_lamports"]
        proceeds = 80_000_000
        native_pre = [0] * len(decoded.static_keys)
        native_post = [0] * len(decoded.static_keys)
        native_pre[0] = 1_000_000_000
        native_post[0] = native_pre[0] + proceeds - fee
        snap = usdc_valuation.snapshot(usdc_valuation.current())
        now = time.time()
        body = {
            "origin": "direct_whirlpool", "confirmed": True, "exit_simulated": True,
            "acquired_raw": acquired, "exit_raw": acquired,
            "exit_message_sha256": hashlib.sha256(solana_tx.message_bytes(encoded)).hexdigest(),
            "exit_token_min_out": quote["token_min_out"], "exit_dex_micro": quote["trade_fee"],
            "exit_pool_micro_per_sol": built["micro_usdc_per_sol"], "exit_valuation": snap,
            "entry_token_min_out": built["quote_token_min_out"], "entry_token_est_out": built["quote_token_est_out"],
            "wallet_usdc_raw": pre_usdc, "ata_balance": pre_usdc,
            "entry_lamports": built["entry_lamports"], "pool_micro_per_sol": built["micro_usdc_per_sol"],
            "dex_fee_lamports": built["dex_fee_lamports"],
            "actual_sol_out_lamports": built["entry_lamports"], "actual_network_fee_lamports": fee,
            "entry_valuation": snap, "entry_price_micro": built["micro_usdc_per_sol"],
            "peak_price_micro": built["micro_usdc_per_sol"], "opened_at": now, "trail_armed": False,
        }
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('exit', 'confirmed', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '8.14', 1, NULL, NULL, '89', ?, ?, ?, ?)
            """,
            (WALLET_ID, WALLET, now, json.dumps(body), now, now),
        )
        expected = usdc_valuation.round_trip_pnl(
            built["entry_lamports"], fee, proceeds, fee, built["micro_usdc_per_sol"], built["micro_usdc_per_sol"], snap, snap,
        )
        return {
            "encoded": encoded, "signature": solana_tx.first_signature(encoded), "slot": bundle["context_slot"],
            "fee": fee, "native": (native_pre, native_post), "expected_pnl": expected["realized_pnl"],
            "entry_price": built["micro_usdc_per_sol"],
            "pre_tokens": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre_usdc)}}],
            "post_tokens": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre_usdc - acquired)}}],
        }
