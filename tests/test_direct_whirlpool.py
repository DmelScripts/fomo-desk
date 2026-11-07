"""Wrapped direct Whirlpool route. No network, no Phantom process, and no send."""
import base64
import unittest

import execution_transport
import phantom_transport
import solana_direct_swap
import solana_tx
import solana_whirlpool
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
        vault_a: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, pool)),
        vault_b: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, pool)),
    }
    accounts[solana_whirlpool.oracle_address(pool)] = (solana_tx.SYSTEM_PROGRAM, b"")
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
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT)
        result = solana_direct_swap.inspect(tx, accounts, BPS, SLOT)
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
        other = solana_direct_swap.build(dear[0], dear[1], dear[2], BPS, LIMIT, BLOCK, SLOT)
        self.assertNotEqual(solana_direct_swap.inspect(other, dear[2], BPS, SLOT)["min_out"], result["min_out"])

    def test_threshold_spend_wrap_and_tail_are_not_caller_claims(self):
        pool, data, accounts, source, dest = _pool()
        good = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT)
        minimum = solana_direct_swap.inspect(good, accounts, BPS, SLOT)["min_out"]
        lying = solana_direct_swap.transaction_from_fields(
            pool, data, source, dest, 10_000_000, 1, LIMIT, True, True, BLOCK, wrap=True,
        )
        over = solana_direct_swap.transaction_from_fields(
            pool, data, source, dest, 10_000_000, minimum, LIMIT, True, True, BLOCK, wrap=True, transfer_lamports=10_000_001,
        )
        classic_tail = solana_direct_swap.transaction_from_fields(
            pool, data, source, dest, 10_000_000, minimum, LIMIT, True, True, BLOCK, wrap=True, extra_account=True,
        )
        self.assertEqual(solana_direct_swap.inspect(lying, accounts, BPS, SLOT)["error_category"], "slippage_unbound")
        self.assertEqual(solana_direct_swap.inspect(over, accounts, BPS, SLOT)["error_category"], "spend_unbound")
        self.assertEqual(solana_direct_swap.inspect(classic_tail, accounts, BPS, SLOT)["error_category"], "adaptive_tail")
        self.assertEqual(solana_direct_swap.inspect(good, accounts, BPS, BLOCK)["error_category"], "quote_context_missing")
        adaptive_pool, adaptive_data, adaptive_accounts, adaptive_source, adaptive_dest = _pool(seed=0)
        adaptive = solana_direct_swap.transaction_from_fields(
            adaptive_pool, adaptive_data, adaptive_source, adaptive_dest, 10_000_000, minimum, LIMIT, True, True, BLOCK,
            wrap=True, extra_account=True,
        )
        checked = solana_direct_swap.inspect(adaptive, adaptive_accounts, BPS, SLOT)
        self.assertEqual(checked["error_category"], "adaptive_fee_unsupported")
        self.assertFalse(checked["byte_checks_passed"])
        stranger = solana_direct_swap._tail_ok(
            [solana_whirlpool.oracle_address(adaptive_pool), solana_tx.b58encode(b"\x99" * 32), adaptive_pool],
            [0, 2, 1], True,
        )
        self.assertEqual(stranger, "adaptive_fee_unsupported")

    def test_route_stays_unmounted_and_the_cli_path_stays_closed(self):
        state = solana_direct_swap.status()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["mounted"])
        self.assertEqual(state["production_path"], "phantom_cli_unchanged")
        self.assertEqual(state["live_quote"], "blocked_fresh_snapshot_not_bound")
        self.assertEqual(state["fee_policy"], "not_approved")
        self.assertFalse(state["aggregate_loss_policy"])
        self.assertEqual(execution_transport.status()["send"], "disabled")
        self.assertEqual(len(execution_transport.SWAP_BLOCKERS), 2)

    def test_threshold_rounds_down_like_orca_core_for_fractional_amounts(self):
        self.assertEqual(solana_direct_swap.exact_in_min_out(101, 100), 99)
        self.assertEqual(solana_direct_swap.exact_in_min_out(897985, 100), 889005)
        for quote, bps in ((True, 100), (2**64, 100), (1, 1), (10, 10000)):
            with self.subTest(quote=quote, bps=bps), self.assertRaises(ValueError):
                solana_direct_swap.exact_in_min_out(quote, bps)

    def test_transaction_permissions_are_checked_before_account_proof(self):
        pool, data, accounts, _source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT)
        raw = base64.b64decode(tx)
        original_readonly = raw[67]
        for readonly in (0, original_readonly + 1):
            altered = bytearray(raw)
            altered[67] = readonly
            shown = solana_direct_swap.inspect(base64.b64encode(altered).decode(), accounts, BPS, SLOT)
            self.assertEqual(shown["error_category"], "whirlpool_account_permissions")
            self.assertFalse(shown["byte_checks_passed"])

    def test_oracle_must_be_read_and_have_the_expected_owner_and_layout(self):
        pool, data, original, _source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, original, BPS, LIMIT, BLOCK, SLOT)
        address = solana_whirlpool.oracle_address(pool)
        for record in (None, (solana_whirlpool.PROGRAM_ID, b""), (solana_tx.SYSTEM_PROGRAM, b"x")):
            accounts = dict(original)
            if record is None:
                del accounts[address]
            else:
                accounts[address] = record
            shown = solana_direct_swap.inspect(tx, accounts, BPS, SLOT)
            self.assertEqual(shown["error_category"], "account_unread" if record is None else "whirlpool_oracle_unbound")
            self.assertFalse(shown["byte_checks_passed"])

    def test_wrap_preserves_the_wallets_existing_token_account(self):
        pool, data, accounts, source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT)
        decoded = solana_tx.decode_transaction(base64.b64decode(tx))
        self.assertEqual([solana_direct_swap._kind(decoded, item) for item in decoded.instructions], ["create", "create", "transfer", "sync", "swap"])
        instructions = [(item.program_index, item.account_indexes, item.data) for item in decoded.instructions]
        instructions.append((decoded.static_keys.index(solana_tx.TOKEN_PROGRAM), [decoded.static_keys.index(source), 0, 0], b"\x09"))
        closing = solana_tx.encode_legacy(WALLET, decoded.static_keys, decoded.readonly_unsigned, instructions, BLOCK)
        shown = solana_direct_swap.inspect(closing, accounts, BPS, SLOT)
        self.assertEqual(shown["error_category"], "existing_account_close_forbidden")

    def test_proven_missing_atas_require_the_exact_prior_creation_plan(self):
        pool, data, original, source, dest = _pool()
        tx = solana_direct_swap.build(pool, data, original, BPS, LIMIT, BLOCK, SLOT)
        accounts = {**original, source: None, dest: None}
        checked = solana_direct_swap.inspect(tx, accounts, BPS, SLOT)
        self.assertTrue(checked["byte_checks_passed"])
        self.assertTrue(checked["token_account_creation_plan_verified"])
        self.assertEqual(checked["missing_wallet_token_account_count"], 2)
        self.assertFalse(checked["live_ready"])
        self.assertIsNone(accounts[source])
        decoded = solana_tx.decode_transaction(base64.b64decode(tx))
        instructions = [(item.program_index, list(item.account_indexes), item.data) for item in decoded.instructions]
        alterations = [instructions[1:], [instructions[2], *instructions[:2], *instructions[3:]]]
        for field, index in ((1, decoded.static_keys.index(dest)), (2, decoded.static_keys.index(dest)), (3, decoded.static_keys.index(USDC))):
            changed = [(program, list(indexes), payload) for program, indexes, payload in instructions]
            changed[0][1][field] = index
            alterations.append(changed)
        alterations.append([(instructions[0][0], instructions[0][1], b"\x00"), *instructions[1:]])
        for changed in alterations:
            altered = solana_tx.encode_legacy(WALLET, decoded.static_keys, decoded.readonly_unsigned, changed, BLOCK)
            with self.subTest(instructions=changed):
                self.assertFalse(solana_direct_swap.inspect(altered, accounts, BPS, SLOT)["byte_checks_passed"])

    def test_creation_plan_never_bypasses_unread_or_invalid_existing_accounts(self):
        pool, data, original, source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, original, BPS, LIMIT, BLOCK, SLOT)
        absent = dict(original)
        del absent[source]
        self.assertEqual(solana_direct_swap.inspect(tx, absent, BPS, SLOT)["error_category"], "account_unread")
        for record in ((None, None), (solana_tx.SYSTEM_PROGRAM, b""), (solana_tx.SYSTEM_PROGRAM, original[source][1]),
                       (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, solana_tx.SYSTEM_PROGRAM)),
                       (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, WALLET))):
            with self.subTest(record=record):
                self.assertFalse(solana_direct_swap.inspect(tx, {**original, source: record}, BPS, SLOT)["byte_checks_passed"])

    def test_caller_slot_does_not_prove_snapshot_freshness_or_live_slippage(self):
        pool, data, accounts, _source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT)
        for slot in (1, SLOT, SLOT + 10_000_000):
            shown = solana_direct_swap.inspect(tx, accounts, BPS, slot)
            self.assertTrue(shown["byte_checks_passed"])
            self.assertTrue(shown["threshold_matches_quote"])
            self.assertFalse(shown["snapshot_verified"])
            self.assertFalse(shown["slippage_verified"])
            self.assertFalse(shown["quote_verified"])
            self.assertFalse(shown["live_ready"])

    def test_truncated_swap_account_list_fails_closed(self):
        pool, data, accounts, _source, _dest = _pool()
        tx = solana_direct_swap.build(pool, data, accounts, BPS, LIMIT, BLOCK, SLOT)
        decoded = solana_tx.decode_transaction(base64.b64decode(tx))
        instructions = [(item.program_index, item.account_indexes, item.data) for item in decoded.instructions]
        program, _indexes, payload = instructions[-1]
        instructions[-1] = (program, [0], payload)
        altered = solana_tx.encode_legacy(WALLET, decoded.static_keys, decoded.readonly_unsigned, instructions, BLOCK)
        self.assertFalse(solana_direct_swap.inspect(altered, accounts, BPS, SLOT)["byte_checks_passed"])

    def test_encoder_does_not_coerce_numbers_or_direction_flags(self):
        for values in ((True, 100, LIMIT, True, True), (10_000_000, 100, LIMIT, 1, True), (10_000_000, 100, 1.5, True, True)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                solana_direct_swap.encode_swap_data(*values)
