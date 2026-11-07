"""Wrapped direct Whirlpool route. No network, no Phantom process, and no send."""
import base64
import json
import unittest
from pathlib import Path

import execution_transport
import phantom_transport
import solana_direct_swap
import solana_tx
import solana_whirlpool
import solana_whirlpool_quote
from tests.test_solana_inspect import BLOCK

WALLET = phantom_transport.REQUIRED_WALLET
NATIVE = solana_tx.NATIVE_MINT
USDC = solana_tx.USDC_MINT
LIMIT = solana_direct_swap.MIN_SQRT_PRICE + 1
SLOT = 466540548
BPS = 100


def _pool(fee_rate=3000, liquidity=10**18, seed=None):
    pool = solana_tx.b58encode(b"\x21" * 32)
    vault_a = solana_tx.b58encode(b"\x31" * 32)
    vault_b = solana_tx.b58encode(b"\x32" * 32)
    data = bytearray(solana_whirlpool.reference_pool(
        NATIVE, vault_a, USDC, vault_b, 8, 0, 1 << 64, fee_rate=fee_rate, liquidity=liquidity,
    ))
    if seed is not None:
        data[solana_whirlpool._TICK_SPACING + 2:solana_whirlpool._TICK_SPACING + 4] = int(seed).to_bytes(2, "little")
    source = solana_tx.associated_token_address(WALLET, NATIVE, solana_tx.TOKEN_PROGRAM)
    dest = solana_tx.associated_token_address(WALLET, USDC, solana_tx.TOKEN_PROGRAM)
    accounts = {
        pool: (solana_whirlpool.PROGRAM_ID, bytes(data)),
        source: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, WALLET)),
        dest: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, WALLET)),
        vault_a: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, solana_tx.b58encode(b"\x91" * 32))),
        vault_b: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, solana_tx.b58encode(b"\x92" * 32))),
    }
    step = solana_whirlpool.TICK_ARRAY_SIZE * 8
    for index in range(3):
        start = -index * step
        address = solana_whirlpool.tick_array_address(pool, start)
        accounts[address] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.reference_tick_array(pool, start))
    return pool, bytes(data), accounts, source, dest


class DirectWhirlpoolTests(unittest.TestCase):
    def test_canonical_pool_is_the_derived_tick_spacing_8_address(self):
        self.assertEqual(solana_direct_swap.derived_sol_usdc_pool(8), (solana_direct_swap.SOL_USDC_POOL, 255))
        self.assertNotEqual(solana_direct_swap.derived_sol_usdc_pool(64)[0], solana_direct_swap.SOL_USDC_POOL)
        observed = solana_direct_swap.SOL_USDC_OBSERVATION
        self.assertTrue(observed["discriminator_ok"])
        self.assertFalse(observed["adaptive_fee"])
        self.assertEqual(observed["fee_tier_seed"], observed["tick_spacing"])
        self.assertEqual(observed["mint_a"], NATIVE)
        self.assertEqual(observed["mint_b"], USDC)
        self.assertEqual(observed["context_slot"], SLOT)
        self.assertFalse(observed["tick_arrays_read"])

    def test_quote_comes_from_pool_bytes_and_wraps_the_wallet_atas(self):
        pool, data, accounts, _source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT, allow_probe=True)
        result = solana_direct_swap.inspect(tx, accounts, BPS, SLOT, allow_probe=True)
        decoded = solana_tx.decode_transaction(base64.b64decode(tx))
        swap = next(item for item in decoded.instructions if decoded.static_keys[item.program_index] == solana_whirlpool.PROGRAM_ID)
        parsed = solana_direct_swap.decode_swap_data(swap.data)
        state = solana_whirlpool.pool_state(data)
        arrays = solana_direct_swap._quote_arrays(pool, data, accounts, True)
        quoted = solana_whirlpool_quote.quote_exact_in(state, arrays, 10_000_000, True, 100, LIMIT, SLOT)
        self.assertEqual(parsed["other_amount_threshold"], quoted["token_min_out"])
        self.assertEqual(result["min_out"], quoted["token_min_out"])
        self.assertTrue(result["byte_checks_passed"])
        self.assertTrue(result["wrap_verified"])
        self.assertTrue(result["output_computed_from_pool"])
        self.assertEqual(result["context_slot"], SLOT)
        self.assertEqual(result["blockhash"], BLOCK)
        self.assertIsInstance(result["context_slot"], int)
        self.assertNotEqual(result["blockhash"], result["context_slot"])
        self.assertFalse(result["enabled"])
        self.assertFalse(result["live_ready"])
        self.assertEqual(result["fee_policy"], "not_approved")
        self.assertFalse(result["aggregate_loss_policy"])
        dear = _pool(fee_rate=10000)
        other = solana_direct_swap.build(dear[0], dear[1], dear[2], BPS, LIMIT, BLOCK, SLOT, allow_probe=True)
        self.assertNotEqual(solana_direct_swap.inspect(other, dear[2], BPS, SLOT, allow_probe=True)["min_out"], result["min_out"])

    def test_threshold_spend_wrap_and_tail_are_not_caller_claims(self):
        pool, data, accounts, source, dest = _pool()
        good = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT, allow_probe=True)
        minimum = solana_direct_swap.inspect(good, accounts, BPS, SLOT, allow_probe=True)["min_out"]
        lying = solana_direct_swap.transaction_from_fields(
            pool, data, source, dest, 10_000_000, 1, LIMIT, True, True, BLOCK, wrap=True,
        )
        over = solana_direct_swap.transaction_from_fields(
            pool, data, source, dest, 10_000_000, minimum, LIMIT, True, True, BLOCK, wrap=True, transfer_lamports=10_000_001,
        )
        classic_tail = solana_direct_swap.transaction_from_fields(
            pool, data, source, dest, 10_000_000, minimum, LIMIT, True, True, BLOCK, wrap=True, extra_account=True,
        )
        self.assertEqual(solana_direct_swap.inspect(lying, accounts, BPS, SLOT, allow_probe=True)["error_category"], "slippage_unbound")
        self.assertEqual(solana_direct_swap.inspect(over, accounts, BPS, SLOT, allow_probe=True)["error_category"], "spend_unbound")
        self.assertEqual(solana_direct_swap.inspect(classic_tail, accounts, BPS, SLOT, allow_probe=True)["error_category"], "adaptive_tail")
        self.assertEqual(solana_direct_swap.inspect(good, accounts, BPS, BLOCK, allow_probe=True)["error_category"], "quote_context_missing")
        adaptive_pool, adaptive_data, adaptive_accounts, adaptive_source, adaptive_dest = _pool(seed=0)
        adaptive = solana_direct_swap.transaction_from_fields(
            adaptive_pool, adaptive_data, adaptive_source, adaptive_dest, 10_000_000, minimum, LIMIT, True, True, BLOCK,
            wrap=True, extra_account=True,
        )
        checked = solana_direct_swap.inspect(adaptive, adaptive_accounts, BPS, SLOT, allow_probe=True)
        self.assertTrue(checked["byte_checks_passed"])
        stranger = solana_direct_swap._tail_ok(
            [solana_whirlpool.oracle_address(adaptive_pool), solana_tx.b58encode(b"\x99" * 32), adaptive_pool],
            [0, 2, 1], True,
        )
        self.assertEqual(stranger, "adaptive_tail")

    def test_route_stays_unmounted_and_the_cli_path_stays_closed(self):
        state = solana_direct_swap.status()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["mounted"])
        self.assertTrue(state["readonly_prepare_mounted"])
        self.assertEqual(state["strategies_enabled"], [])
        self.assertEqual(state["production_path"], "phantom_cli_unchanged")
        self.assertEqual(state["live_quote"], "computed")
        self.assertEqual(state["specified_slippage_bps"], 100)
        self.assertEqual(state["fee_policy"], "not_approved")
        self.assertFalse(state["aggregate_loss_policy"])
        self.assertEqual(len(execution_transport.SWAP_BLOCKERS), 2)

    def test_string_seed_arrays_match_the_sdk_quote(self):
        snap = solana_direct_swap.snapshot_status()
        self.assertEqual(snap["quote"], "computed")
        self.assertIsNone(snap["reason"])
        self.assertEqual(solana_direct_swap.SOL_USDC_SNAPSHOT["liquidity"], 576950119138)
        self.assertTrue(snap["tick_arrays_present"])
        self.assertEqual(snap["context_slot"], 466547240)
        pool = solana_direct_swap.SOL_USDC_POOL
        program = solana_tx.b58decode(solana_whirlpool.PROGRAM_ID)
        raw_pool = solana_tx.b58decode(pool)
        with open(Path(__file__).resolve().parents[1] / "fixtures" / "sol_usdc_ts8_ticks.json", encoding="utf-8") as handle:
            body = json.load(handle)
        arrays = []
        for arr, address in zip(body["arrays"], solana_direct_swap.SOL_USDC_SNAPSHOT["tick_array_addresses"]):
            self.assertEqual(solana_whirlpool.tick_array_address(pool, arr["start"]), address)
            binary, _bump = solana_tx.find_program_address(
                [b"tick_array", raw_pool, int(arr["start"]).to_bytes(4, "little", signed=True)], program,
            )
            self.assertNotEqual(solana_tx.b58encode(binary), address)
            raw = bytearray(solana_whirlpool.reference_tick_array(pool, arr["start"]))
            for tick, net in arr["initialized"]:
                offset = 12 + ((tick - arr["start"]) // body["tickSpacing"]) * 113
                raw[offset] = 1
                raw[offset + 1:offset + 17] = int(net).to_bytes(16, "little", signed=True)
            parsed = solana_whirlpool.tick_array_state(bytes(raw))
            self.assertEqual(parsed["start_tick_index"], arr["start"])
            self.assertEqual(parsed["pool"], pool)
            arrays.append(parsed)
        quoted = solana_whirlpool_quote.quote_exact_in({
            "sqrt_price": int(body["sqrtPrice"]),
            "tick_current_index": body["tickCurrentIndex"],
            "fee_rate": body["feeRate"],
            "liquidity": int(body["liquidity"]),
            "tick_spacing": body["tickSpacing"],
        }, arrays, 10_000_000, True, 100, 0, body["slot"])
        self.assertEqual(quoted["token_est_out"], 898631)
        self.assertEqual(quoted["token_min_out"], 889644)
        self.assertEqual(quoted["trade_fee"], 5000)
        pool_state = {
            "sqrt_price": int(body["sqrtPrice"]),
            "tick_current_index": body["tickCurrentIndex"],
            "fee_rate": body["feeRate"],
            "liquidity": int(body["liquidity"]),
            "tick_spacing": body["tickSpacing"],
        }
        entry = solana_whirlpool_quote.quote_exact_in(pool_state, arrays, 88979314, True, 100, 0, body["slot"])
        self.assertEqual((entry["token_est_out"], entry["token_min_out"], entry["trade_fee"]), (7995630, 7915673, 44490))
        exited = solana_direct_swap.quote_exit_exact_in(pool_state, arrays, entry["token_est_out"], 100, body["slot"])
        self.assertEqual((exited["token_est_out"], exited["token_min_out"], exited["trade_fee"]), (88882136, 87993314, 3998))
        self.assertFalse(exited["future_exit_guaranteed"])
        size = solana_direct_swap.sizing()
        self.assertEqual(size["probe_usdc"], "0.898631")
        self.assertEqual(size["entry_lamports"], 88979314)
        self.assertEqual(size["unspent_micro_usdc"], 1)
        self.assertLess(size["quoted_out_micro_usdc"], size["marginal_value_micro_usdc"])
        self.assertFalse(size["probe_is_ticket"])
        self.assertFalse(size["usd_spend_is_exact"])
        self.assertEqual(solana_direct_swap.lamports_within_usd_cap(0, size["marginal_micro_usdc_per_sol"]), 0)
        self.assertEqual(solana_direct_swap.lamports_within_usd_cap(8_000_000, 10**18), 0)
        self.assertFalse(solana_direct_swap.context_binds(body["slot"] - 1, body["slot"]))
        self.assertTrue(snap["context_fresh"])
        self.assertFalse(solana_direct_swap.context_binds(466540548, 466542559))
        self.assertFalse(solana_direct_swap.context_binds(466540547, 466542559))
        self.assertTrue(snap["close_wsol"])
        self.assertFalse(snap["close_usdc"])
        self.assertFalse(solana_direct_swap.close_allowed(True))
        self.assertTrue(solana_direct_swap.explicit_rent_allowed(1488440))
        self.assertFalse(solana_direct_swap.explicit_rent_allowed(1488441))
        self.assertEqual(solana_direct_swap.RENT_EXEMPTION_165, snap["rent_exemption_165"])
        self.assertEqual(solana_direct_swap.SOL_USDC_SNAPSHOT["wallet_usdc_ata_lamports"], 1488440)
        cycle = solana_direct_swap.paper_lifecycle()
        self.assertFalse(cycle["entry_opened"])
        self.assertFalse(cycle["exit_armed"])
        self.assertFalse(cycle["position_written"])
        self.assertEqual(cycle["chat_conditional_live_approval"], "not_a_control")
        self.assertFalse(cycle["five_agents"])

    def test_existing_wsol_balance_is_not_closed_and_token_2022_is_rejected(self):
        pool, data, accounts, source, dest = _pool()
        self.assertFalse(solana_direct_swap.close_allowed(True))
        raw = bytearray(accounts[source][1])
        raw[64:72] = (50_000).to_bytes(8, "little")
        accounts[source] = (solana_tx.TOKEN_PROGRAM, bytes(raw))
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT, allow_probe=True)
        self.assertTrue(solana_direct_swap.inspect(tx, accounts, BPS, SLOT, allow_probe=True)["byte_checks_passed"])
        foreign = solana_tx.b58encode(b"\x58" * 32)
        accounts[source] = (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, foreign))
        owned = solana_direct_swap.inspect(tx, accounts, BPS, SLOT, allow_probe=True)
        self.assertEqual(owned["error_category"], "whirlpool_owner_unbound")
        accounts[source] = (solana_tx.TOKEN_2022, solana_whirlpool.reference_token_account(NATIVE, WALLET))
        self.assertEqual(solana_direct_swap.inspect(tx, accounts, BPS, SLOT, allow_probe=True)["error_category"], "token_2022_unsupported")
        self.assertEqual(solana_direct_swap.inspect(tx, accounts, BPS, SLOT, wsol_existed=True, allow_probe=True)["error_category"], "preexisting_wsol")
