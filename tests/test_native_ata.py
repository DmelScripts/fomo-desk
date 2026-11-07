"""Same-transaction native wSOL account creation. No network."""
import json
import subprocess
import unittest

import phantom_transport
import solana_inspect
import solana_tx
from tests.test_solana_inspect import BLOCK, FakeRpc

WALLET = phantom_transport.REQUIRED_WALLET
NATIVE = solana_tx.NATIVE_MINT
USDC = solana_tx.USDC_MINT
TOKEN = solana_tx.TOKEN_PROGRAM
SYSTEM = solana_tx.SYSTEM_PROGRAM
ATA_PROGRAM = solana_tx.ASSOCIATED_TOKEN


def _ata(owner, mint, token_program=TOKEN):
    return solana_tx.associated_token_address(owner, mint, token_program)


def _tx(instructions, keys=None):
    chosen = keys or [WALLET, _ata(WALLET, NATIVE), NATIVE, SYSTEM, TOKEN, ATA_PROGRAM]
    return solana_tx.encode_legacy(WALLET, chosen, 4, instructions, BLOCK)


def _create(ata_index=1, owner_index=0, mint_index=2, token_index=4):
    return (5, [0, ata_index, owner_index, mint_index, 3, token_index], bytes([1]))


class NativeAtaTests(unittest.TestCase):
    def test_address_matches_the_spl_derivation(self):
        node = subprocess.check_output(["node", "--input-type=module", "-e", """
import { createRequire } from 'module';
const require = createRequire(process.cwd() + '/package.json');
const { PublicKey } = require('@solana/web3.js');
const [ata] = PublicKey.findProgramAddressSync([
  new PublicKey('%s').toBuffer(),
  new PublicKey('%s').toBuffer(),
  new PublicKey('%s').toBuffer(),
], new PublicKey('%s'));
console.log(JSON.stringify({ata: ata.toBase58()}));
""" % (WALLET, TOKEN, NATIVE, ATA_PROGRAM)], text=True)
        self.assertEqual(_ata(WALLET, NATIVE), json.loads(node)["ata"])

    def test_create_then_transfer_sync_and_close_does_not_need_preexistence(self):
        ata = _ata(WALLET, NATIVE)
        transfer = (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")
        tx = _tx([
            _create(),
            (3, [0, 1], transfer),
            (4, [1], bytes([17])),
            (4, [1, 0, 0], bytes([9])),
        ])
        fake = FakeRpc()
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["stage"], "chain_checked")
        self.assertEqual(shown["debited_lamports"], 10_000_000)
        self.assertNotIn(ata, fake.tables)
        self.assertNotIn("getAccountInfo", [method for method in fake.methods if method == "getAccountInfo"])
        self.assertFalse(shown["spend_verified"])
        self.assertFalse(shown["execution_allowed"])

    def test_sync_without_an_earlier_create_stays_unread(self):
        tx = _tx([(4, [1], bytes([17]))])
        fake = FakeRpc()
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "account_unread")
        self.assertEqual(shown["simulation_status"], "not_run")

    def test_create_after_sync_does_not_cover_the_sync(self):
        tx = _tx([
            (4, [1], bytes([17])),
            _create(),
        ])
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "account_unread")

    def test_usdc_create_does_not_authorize_a_native_sync(self):
        usdc_ata = _ata(WALLET, USDC)
        keys = [WALLET, usdc_ata, USDC, SYSTEM, TOKEN, ATA_PROGRAM]
        tx = solana_tx.encode_legacy(WALLET, keys, 4, [
            (5, [0, 1, 0, 2, 3, 4], bytes([1])),
            (4, [1], bytes([17])),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "account_unread")
        self.assertNotEqual(usdc_ata, _ata(WALLET, NATIVE))

    def test_wrong_pda_is_not_a_created_native_account(self):
        wrong = solana_tx.b58encode(b"\x44" * 32)
        keys = [WALLET, wrong, NATIVE, SYSTEM, TOKEN, ATA_PROGRAM]
        tx = solana_tx.encode_legacy(WALLET, keys, 4, [
            (5, [0, 1, 0, 2, 3, 4], bytes([1])),
            (4, [1], bytes([17])),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "account_unread")

    def test_unbound_owner_is_not_propagated(self):
        stranger = solana_tx.b58encode(b"\x46" * 32)
        ata = _ata(stranger, NATIVE)
        keys = [WALLET, ata, stranger, NATIVE, SYSTEM, TOKEN, ATA_PROGRAM]
        tx = solana_tx.encode_legacy(WALLET, keys, 5, [
            (6, [0, 1, 2, 3, 4, 5], bytes([1])),
            (5, [1], bytes([17])),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "wallet_unbound")

    def test_close_to_a_third_party_stays_unbound(self):
        stranger = solana_tx.b58encode(b"\x47" * 32)
        keys = [WALLET, _ata(WALLET, NATIVE), NATIVE, SYSTEM, TOKEN, ATA_PROGRAM, stranger]
        tx = solana_tx.encode_legacy(WALLET, keys, 5, [
            _create(),
            (4, [1, 6, 0], bytes([9])),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "close_destination_unbound")

    def test_sync_of_a_different_account_is_not_the_created_ata(self):
        other = solana_tx.b58encode(b"\x48" * 32)
        keys = [WALLET, _ata(WALLET, NATIVE), NATIVE, SYSTEM, TOKEN, ATA_PROGRAM, other]
        tx = solana_tx.encode_legacy(WALLET, keys, 4, [
            _create(),
            (4, [6], bytes([17])),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "account_unread")
