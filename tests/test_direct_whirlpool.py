"""Direct Whirlpool swap bytes. No network, no Phantom process, and no send."""
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
QUOTE = 1_500_000
BPS = 50
MINIMUM = QUOTE - QUOTE * BPS // 10_000
LIMIT = solana_direct_swap.MIN_SQRT_PRICE + 1
SQRT = solana_direct_swap._TICK_ZERO_SQRT


def _pool(mint_a=NATIVE, mint_b=USDC):
    pool = solana_tx.b58encode(b"\x21" * 32)
    source = solana_tx.b58encode(b"\x66" * 32)
    dest = solana_tx.b58encode(b"\x69" * 32)
    vault_a = solana_tx.b58encode(b"\x31" * 32)
    vault_b = solana_tx.b58encode(b"\x32" * 32)
    data = solana_whirlpool.reference_pool(mint_a, vault_a, mint_b, vault_b, 8, 0, SQRT)
    return pool, source, dest, data


def _accounts(pool, source, dest, data, source_owner=WALLET):
    mint_a, vault_a, mint_b, vault_b = solana_whirlpool.pool_fields(data)
    spacing = 8
    current = 0
    base = solana_whirlpool.start_tick_index(current, spacing)
    step = solana_whirlpool.TICK_ARRAY_SIZE * spacing
    a_to_b = mint_a == NATIVE
    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
    accounts = {
        pool: (solana_whirlpool.PROGRAM_ID, data),
        source: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, source_owner)),
        dest: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, WALLET)),
        vault_a: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(mint_a, solana_tx.b58encode(b"\x91" * 32))),
        vault_b: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(mint_b, solana_tx.b58encode(b"\x92" * 32))),
    }
    for start in starts:
        accounts[solana_whirlpool.tick_array_address(pool, start)] = (
            solana_whirlpool.PROGRAM_ID,
            solana_whirlpool.reference_tick_array(pool, start),
        )
    return accounts


class DirectWhirlpoolTests(unittest.TestCase):
    def setUp(self):
        self.pool, self.source, self.dest, self.data = _pool()
        self.accounts = _accounts(self.pool, self.source, self.dest, self.data)

    def test_discriminator_and_field_order_match_the_published_encoder(self):
        self.assertEqual(solana_direct_swap.DISC, solana_direct_swap.PUBLISHED_DISC)
        data = solana_direct_swap.encode_swap_data(10_000_000, MINIMUM, LIMIT, True, True)
        self.assertEqual(len(data), 42)
        self.assertEqual(list(data[:8]), [248, 198, 158, 145, 225, 117, 135, 200])
        self.assertEqual(int.from_bytes(data[8:16], "little"), 10_000_000)
        self.assertEqual(int.from_bytes(data[16:24], "little"), MINIMUM)
        self.assertEqual(int.from_bytes(data[24:40], "little"), LIMIT)
        self.assertEqual(data[40:], bytes([1, 1]))
        self.assertEqual(solana_direct_swap.exact_in_min_out(QUOTE, BPS), MINIMUM)

    def test_builder_binds_min_out_spend_and_wallet_from_the_instruction(self):
        tx = solana_direct_swap.build(self.pool, self.data, self.source, self.dest, QUOTE, BPS, LIMIT, BLOCK)
        result = solana_direct_swap.inspect(tx, self.accounts, QUOTE, BPS)
        self.assertTrue(result["byte_checks_passed"])
        self.assertTrue(result["spend_verified"])
        self.assertTrue(result["slippage_verified"])
        self.assertTrue(result["wallet_verified"])
        self.assertEqual(result["min_out"], MINIMUM)
        self.assertFalse(result["enabled"])
        self.assertFalse(result["mounted"])
        self.assertFalse(result["output_computed_from_pool"])
        self.assertEqual(result["fee_policy"], "not_approved")
        self.assertFalse(result["aggregate_loss_policy"])
        self.assertFalse(result["live_ready"])

    def test_a_caller_min_out_claim_cannot_override_the_bytes(self):
        lying = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, 1, LIMIT, True, True, BLOCK,
        )
        result = solana_direct_swap.inspect(lying, self.accounts, QUOTE, BPS)
        self.assertEqual(result["min_out"], 1)
        self.assertEqual(result["error_category"], "slippage_unbound")
        self.assertFalse(result["slippage_verified"])
        unprotected = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, 0, LIMIT, True, True, BLOCK,
        )
        self.assertEqual(solana_direct_swap.inspect(unprotected, self.accounts, QUOTE, BPS)["error_category"], "slippage_unbound")

    def test_spend_direction_wallet_and_extra_accounts_are_rejected(self):
        over = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_001, MINIMUM, LIMIT, True, True, BLOCK,
        )
        exact_out = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, MINIMUM, LIMIT, False, True, BLOCK,
        )
        stranger = solana_tx.b58encode(b"\x44" * 32)
        foreign = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, MINIMUM, LIMIT, True, True, BLOCK,
            token_authority=stranger,
        )
        flipped = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, MINIMUM, LIMIT, True, False, BLOCK,
        )
        wider = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, MINIMUM, LIMIT, True, True, BLOCK,
            extra_account=True,
        )
        second = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, MINIMUM, LIMIT, True, True, BLOCK,
            copies=2,
        )
        wrong_limit = solana_direct_swap.transaction_from_fields(
            self.pool, self.data, self.source, self.dest, 10_000_000, MINIMUM,
            solana_direct_swap.MAX_SQRT_PRICE - 1, True, True, BLOCK,
        )
        self.assertEqual(solana_direct_swap.inspect(over, self.accounts, QUOTE, BPS)["error_category"], "spend_unbound")
        self.assertEqual(solana_direct_swap.inspect(exact_out, self.accounts, QUOTE, BPS)["error_category"], "spend_unbound")
        self.assertEqual(solana_direct_swap.inspect(foreign, self.accounts, QUOTE, BPS)["error_category"], "whirlpool_authority_unbound")
        self.assertEqual(solana_direct_swap.inspect(flipped, self.accounts, QUOTE, BPS)["error_category"], "direction_unbound")
        self.assertEqual(solana_direct_swap.inspect(wider, self.accounts, QUOTE, BPS)["error_category"], "swap_data_unbound")
        self.assertEqual(solana_direct_swap.inspect(second, self.accounts, QUOTE, BPS)["error_category"], "transaction_shape")
        self.assertEqual(solana_direct_swap.inspect(wrong_limit, self.accounts, QUOTE, BPS)["error_category"], "sqrt_price_limit")

    def test_reversed_pool_still_spends_sol_and_the_route_stays_unmounted(self):
        pool, source, dest, data = _pool(USDC, NATIVE)
        accounts = _accounts(pool, source, dest, data)
        limit = solana_direct_swap.MAX_SQRT_PRICE - 1
        tx = solana_direct_swap.build(pool, data, source, dest, QUOTE, BPS, limit, BLOCK)
        result = solana_direct_swap.inspect(tx, accounts, QUOTE, BPS)
        self.assertTrue(result["byte_checks_passed"])
        self.assertEqual(result["min_out"], MINIMUM)
        state = solana_direct_swap.status()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["mounted"])
        self.assertEqual(state["production_path"], "phantom_cli_unchanged")
        self.assertFalse(state["output_computed_from_pool"])
        self.assertEqual(state["adaptive_fee_extra_account"], "not_verified")
        self.assertEqual(state["wrapped_sol_setup"], "not_in_this_transaction")
        self.assertEqual(execution_transport.status()["send"], "disabled")
        self.assertEqual(len(execution_transport.SWAP_BLOCKERS), 2)
