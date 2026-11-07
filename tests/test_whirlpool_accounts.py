"""Independent Whirlpool account checks. No network and no signing."""
import base64
import json
import subprocess
import unittest

import jupiter_build
import solana_inspect
import solana_jupiter
import solana_tx
import solana_whirlpool
from tests.test_jupiter_build import TABLE, TOKEN, USDC, WALLET, _account, _body, _ix, _rpc
from tests.test_solana_inspect import FakeRpc

NATIVE = solana_tx.NATIVE_MINT


def _keys(**replace):
    pool = solana_tx.b58encode(b"\x21" * 32)
    source = solana_tx.b58encode(b"\x66" * 32)
    dest = solana_tx.b58encode(b"\x69" * 32)
    vault_a = solana_tx.b58encode(b"\x31" * 32)
    vault_b = solana_tx.b58encode(b"\x32" * 32)
    spacing, current = 8, 16
    base = solana_whirlpool.start_tick_index(current, spacing)
    step = solana_whirlpool.TICK_ARRAY_SIZE * spacing
    starts = [base - i * step for i in range(3)]
    ticks = [solana_whirlpool.tick_array_address(pool, start) for start in starts]
    oracle = solana_whirlpool.oracle_address(pool)
    values = {
        "pool": pool, "source": source, "dest": dest, "vault_a": vault_a, "vault_b": vault_b,
        "ticks": ticks, "starts": starts, "oracle": oracle, "authority": WALLET,
        "token_program": solana_tx.TOKEN_PROGRAM,
    }
    values.update(replace)
    keys = [
        values["token_program"], values["authority"], values["pool"], values["source"], values["vault_a"],
        values["dest"], values["vault_b"], *values["ticks"], values["oracle"],
    ]
    pool_data = solana_whirlpool.reference_pool(NATIVE, values["vault_a"], USDC, values["vault_b"], spacing, current)
    accounts = {
        values["pool"]: (solana_whirlpool.PROGRAM_ID, pool_data),
        values["source"]: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, WALLET)),
        values["dest"]: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, WALLET)),
        values["vault_a"]: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(NATIVE, values["pool"])),
        values["vault_b"]: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, values["pool"])),
    }
    for address, start in zip(values["ticks"], values["starts"]):
        accounts[address] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.reference_tick_array(values["pool"], start))
    return keys, list(range(11)), accounts, values


class WhirlpoolAccountTests(unittest.TestCase):
    def test_tick_array_discriminator_and_oracle_match_published_values(self):
        self.assertEqual(solana_whirlpool._TICK_DISC.hex(), "4561bdbe6e0742bb")
        pool = solana_tx.b58encode(b"\x21" * 32)
        node = subprocess.check_output(["node", "--input-type=module", "-e", """
import { createRequire } from 'module';
const require = createRequire(process.cwd() + '/package.json');
const { PublicKey } = require('@solana/web3.js');
const pool = new PublicKey('%s');
const program = new PublicKey('%s');
const [oracle] = PublicKey.findProgramAddressSync([Buffer.from('oracle'), pool.toBuffer()], program);
const start = Buffer.from('-704'); // Published Orca seed is a decimal string.
const [tick] = PublicKey.findProgramAddressSync([Buffer.from('tick_array'), pool.toBuffer(), start], program);
console.log(JSON.stringify({oracle: oracle.toBase58(), tick: tick.toBase58()}));
""" % (pool, solana_whirlpool.PROGRAM_ID)], text=True)
        found = json.loads(node)
        self.assertEqual(solana_whirlpool.oracle_address(pool), found["oracle"])
        self.assertEqual(solana_whirlpool.tick_array_address(pool, -704), found["tick"])

    def test_reference_layout_is_accepted(self):
        keys, remaining, accounts, _values = _keys()
        solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)

    def test_tick_array_pda_does_not_use_binary_integer_seed(self):
        pool = solana_tx.b58encode(b"\x21" * 32)
        wrong, _ = solana_tx.find_program_address(
            [b"tick_array", solana_tx.b58decode(pool), (-704).to_bytes(4, "little", signed=True)],
            solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
        )
        self.assertNotEqual(solana_whirlpool.tick_array_address(pool, -704), solana_tx.b58encode(wrong))

    def test_truncated_pool_and_wrong_token_program_owner_are_rejected(self):
        keys, remaining, accounts, values = _keys()
        pool_owner, pool_data = accounts[values["pool"]]
        accounts[values["pool"]] = (pool_owner, pool_data[:269])
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_pool_unbound")
        accounts[values["pool"]] = (pool_owner, pool_data)
        accounts[values["source"]] = (solana_tx.SYSTEM_PROGRAM, accounts[values["source"]][1])
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_owner_unbound")

    def test_vault_authority_and_token_state_are_not_ignored(self):
        for target, offset, value, category in (("vault_a", 32, 0x91, "whirlpool_vault_unbound"), ("source", 108, 0, "whirlpool_owner_unbound"), ("dest", 108, 2, "whirlpool_owner_unbound")):
            keys, remaining, accounts, values = _keys()
            owner, data = accounts[values[target]]
            data = bytearray(data)
            data[offset] = value
            accounts[values[target]] = (owner, bytes(data))
            with self.subTest(target=target), self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
                solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
            self.assertEqual(caught.exception.category, category)

    def test_lowest_tick_array_start_uses_floor_division_once(self):
        spacing = 8
        lowest = solana_whirlpool.MIN_TICK_INDEX // (88 * spacing) * (88 * spacing)
        self.assertTrue(solana_whirlpool._valid_start(lowest, spacing))
        self.assertFalse(solana_whirlpool._valid_start(lowest - 88 * spacing, spacing))

    def test_substituted_vault_is_rejected(self):
        keys, remaining, accounts, values = _keys()
        keys[4] = solana_tx.b58encode(b"\x39" * 32)
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_vault_unbound")

    def test_direction_mint_swap_is_rejected(self):
        keys, remaining, accounts, _values = _keys()
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, False, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_mint_unbound")

    def test_misordered_tick_array_is_rejected(self):
        keys, remaining, accounts, _values = _keys()
        keys[7], keys[8] = keys[8], keys[7]
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_tick_array_unbound")

    def test_tick_array_for_another_pool_is_rejected(self):
        keys, remaining, accounts, values = _keys()
        other = solana_tx.b58encode(b"\x44" * 32)
        accounts[values["ticks"][0]] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.reference_tick_array(other, values["starts"][0]))
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_tick_array_unbound")

    def test_bad_tick_array_discriminator_is_rejected(self):
        keys, remaining, accounts, values = _keys()
        data = bytearray(accounts[values["ticks"][1]][1])
        data[0] ^= 0xFF
        accounts[values["ticks"][1]] = (solana_whirlpool.PROGRAM_ID, bytes(data))
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_tick_array_unbound")

    def test_wrong_program_owner_is_rejected(self):
        keys, remaining, accounts, values = _keys()
        accounts[values["pool"]] = (solana_tx.SYSTEM_PROGRAM, accounts[values["pool"]][1])
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_pool_unbound")

    def test_user_token_mint_substitution_is_rejected(self):
        keys, remaining, accounts, values = _keys()
        accounts[values["source"]] = (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(USDC, WALLET))
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_owner_unbound")

    def test_authority_must_be_the_route_user_not_a_guessed_pda(self):
        keys, remaining, accounts, _values = _keys(authority=solana_tx.b58encode(b"\x41" * 32))
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_authority_unbound")

    def test_program_id_prefix_is_not_stripped(self):
        keys, remaining, accounts, _values = _keys()
        prefixed = [0] + [index + 1 for index in remaining]
        keys = [solana_whirlpool.PROGRAM_ID] + keys
        shifted = {}
        for key, record in list(accounts.items()):
            shifted[key] = record
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, prefixed, WALLET, keys[4], keys[6], NATIVE, USDC, True, shifted)
        self.assertEqual(caught.exception.category, "whirlpool_accounts_unbound")
        self.assertEqual(len(prefixed), 12)

    def test_bad_oracle_pda_is_rejected(self):
        keys, remaining, accounts, _values = _keys()
        keys[10] = solana_tx.b58encode(b"\x58" * 32)
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_oracle_unbound")

    def test_current_tick_outside_the_first_array_is_rejected(self):
        keys, remaining, accounts, values = _keys()
        data = bytearray(accounts[values["pool"]][1])
        data[solana_whirlpool._TICK_CURRENT:solana_whirlpool._TICK_CURRENT + 4] = (800).to_bytes(4, "little", signed=True)
        accounts[values["pool"]] = (solana_whirlpool.PROGRAM_ID, bytes(data))
        with self.assertRaises(solana_whirlpool.WhirlpoolError) as caught:
            solana_whirlpool.check_swap_accounts(keys, remaining, WALLET, keys[3], keys[5], NATIVE, USDC, True, accounts)
        self.assertEqual(caught.exception.category, "whirlpool_tick_array_unbound")


class WhirlpoolRouteTests(unittest.TestCase):
    def _instruction(self, keys, data):
        metas = []
        for index, key in enumerate(keys):
            metas.append(_account(key, signer=key == WALLET, writable=key != solana_tx.TOKEN_PROGRAM))
        return _ix(solana_jupiter.PROGRAM_ID, metas, data)

    def _data(self, variant=17):
        return (
            solana_jupiter.ROUTE_V2
            + (10_000_000).to_bytes(8, "little")
            + (897985).to_bytes(8, "little")
            + (100).to_bytes(2, "little")
            + (0).to_bytes(4, "little")
            + (1).to_bytes(4, "little")
            + bytes([variant, 1])
            + (10_000).to_bytes(2, "little")
            + bytes([0, 1])
        )

    def _load(self, fake, values, accounts):
        for address, (owner, data) in accounts.items():
            fake.tables[address] = data
            fake.owners[address] = owner

    def test_checked_whirlpool_route_keeps_execution_false(self):
        _keys_list, _remaining, accounts, values = _keys()
        fixed = [
            WALLET, values["source"], values["dest"], NATIVE, USDC, TOKEN, TOKEN,
            values["dest"], solana_tx.b58encode(b"\x70" * 32), solana_jupiter.PROGRAM_ID,
        ]
        body = _body(outAmount="897985", otherAmountThreshold="889006")
        body["swapInstruction"] = self._instruction(fixed + _keys_list, self._data())
        body["addressesByLookupTableAddress"] = {TABLE: [values["pool"]]}
        fake = _rpc()
        meta = (1).to_bytes(4, "little") + (b"\xff" * 8) + (b"\x00" * 44)
        fake.tables[TABLE] = meta + solana_tx.b58decode(values["pool"])
        self._load(fake, values, accounts)
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["stage"], "chain_checked")
        self.assertTrue(shown["chain_ok"])
        self.assertTrue(shown["dex_accounts_verified"])
        self.assertFalse(shown["slippage_verified"])
        self.assertFalse(shown["min_output_verified"])
        self.assertEqual(shown["slippage_propagation"], "not_published")
        self.assertTrue(shown["threshold_matches_ceil"])
        self.assertFalse(shown["spend_verified"])
        self.assertFalse(shown["execution_allowed"])
        self.assertFalse(shown["fee_authorized"])
        self.assertFalse(shown["send"])
        self.assertIn("simulateTransaction", fake.methods)

    def test_token2022_is_rejected_before_simulation(self):
        _keys_list, _remaining, accounts, values = _keys()
        fixed = [
            WALLET, values["source"], values["dest"], NATIVE, USDC,
            solana_tx.TOKEN_2022, TOKEN, values["dest"], solana_tx.b58encode(b"\x70" * 32),
            solana_jupiter.PROGRAM_ID,
        ]
        body = _body(outAmount="897985", otherAmountThreshold="889006")
        body["swapInstruction"] = self._instruction(fixed + _keys_list, self._data())
        fake = FakeRpc()
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "token_2022_unsupported")
        self.assertNotIn("simulateTransaction", fake.methods)

    def test_unrecognized_hop_is_rejected(self):
        _keys_list, _remaining, accounts, values = _keys()
        fixed = [
            WALLET, values["source"], values["dest"], NATIVE, USDC, TOKEN, TOKEN,
            values["dest"], solana_tx.b58encode(b"\x70" * 32), solana_jupiter.PROGRAM_ID,
        ]
        body = _body(outAmount="897985", otherAmountThreshold="889006")
        body["swapInstruction"] = self._instruction(fixed + _keys_list, self._data(variant=120))
        fake = FakeRpc()
        self._load(fake, values, accounts)
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "unsupported_route")
        self.assertNotIn("simulateTransaction", fake.methods)
