"""Jupiter Metis /build fixtures. No network, no route, and no signing."""
import base64
import unittest

import api
import jupiter_build
import phantom_transport
import solana_inspect
import solana_jupiter
import solana_tx
import solana_whirlpool
from tests.test_solana_inspect import BLOCK, FakeRpc, WALLET

RENT = solana_tx.TOKEN_ACCOUNT_RENT
OUT = 897031
FLOOR = OUT * 9900 // 10_000
DEX = solana_tx.b58encode(b"\x99" * 32)
TABLE = solana_tx.b58encode(b"\x33" * 32)
NEW = solana_tx.b58encode(b"\x44" * 32)
TOKEN = solana_tx.TOKEN_PROGRAM
NATIVE = solana_tx.NATIVE_MINT
USDC = solana_tx.USDC_MINT


def _account(pubkey, signer=False, writable=False):
    return {"pubkey": pubkey, "isSigner": signer, "isWritable": writable}


def _ix(program, accounts, data):
    return {"programId": program, "accounts": accounts, "data": base64.b64encode(data).decode("ascii")}


def _route_data():
    plan = (1).to_bytes(4, "little") + bytes([7, 100, 0, 1])
    return (
        solana_jupiter._SHARED + bytes([1]) + plan
        + (10_000_000).to_bytes(8, "little")
        + OUT.to_bytes(8, "little")
        + (100).to_bytes(2, "little")
        + bytes([0])
    )


def _body(**overrides):
    limit = bytes([2]) + (200_000).to_bytes(4, "little")
    price = bytes([3]) + (0).to_bytes(8, "little")
    transfer = (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")
    create = (
        (0).to_bytes(4, "little")
        + RENT.to_bytes(8, "little")
        + (165).to_bytes(8, "little")
        + solana_tx.b58decode(TOKEN)
    )
    swap_accounts = [
        _account(TOKEN),
        _account(solana_tx.b58encode(b"\x55" * 32), writable=True),
        _account(WALLET, signer=True, writable=True),
        _account(solana_tx.b58encode(b"\x66" * 32), writable=True),
        _account(solana_tx.b58encode(b"\x67" * 32), writable=True),
        _account(solana_tx.b58encode(b"\x68" * 32), writable=True),
        _account(solana_tx.b58encode(b"\x69" * 32), writable=True),
        _account(NATIVE),
        _account(USDC),
        _account(DEX, writable=True),
    ]
    body = {
        "inputMint": NATIVE,
        "outputMint": USDC,
        "inAmount": "10000000",
        "outAmount": str(OUT),
        "otherAmountThreshold": str(FLOOR),
        "swapMode": "ExactIn",
        "slippageBps": 100,
        "priceImpactPct": "0.001",
        "transactionVersion": 0,
        "routePlan": [{
            "percent": 100,
            "bps": 10000,
            "swapInfo": {
                "ammKey": DEX,
                "label": "fixture",
                "inputMint": NATIVE,
                "outputMint": USDC,
                "inAmount": "10000000",
                "outAmount": str(OUT),
            },
        }],
        "computeBudgetInstructions": [
            _ix(solana_tx.COMPUTE_BUDGET, [], limit),
            _ix(solana_tx.COMPUTE_BUDGET, [], price),
        ],
        "setupInstructions": [
            _ix(solana_tx.SYSTEM_PROGRAM, [_account(WALLET, signer=True, writable=True), _account(WALLET, writable=True)], transfer),
            _ix(solana_tx.SYSTEM_PROGRAM, [_account(WALLET, signer=True, writable=True), _account(NEW, writable=True)], create),
        ],
        "swapInstruction": _ix(solana_jupiter.PROGRAM_ID, swap_accounts, _route_data()),
        "cleanupInstruction": None,
        "otherInstructions": [],
        "tipInstruction": None,
        "addressesByLookupTableAddress": {TABLE: [DEX]},
        "blockhashWithMetadata": {
            "blockhash": list(b"\x11" * 32),
            "lastValidBlockHeight": 250_000_000,
            "fetchedAt": {"secs_since_epoch": 1, "nanos_since_epoch": 0},
        },
    }
    body.update(overrides)
    return body


class _Response:
    def __init__(self, body, status=200, url=None, rpc=None):
        self.body = body
        self.status = status
        self.url = url if url is not None else jupiter_build.pinned_request()["url"]
        self.rpc = rpc


def _rpc():
    fake = FakeRpc()
    meta = (1).to_bytes(4, "little") + (b"\xff" * 8) + (b"\x00" * 44)
    fake.tables[TABLE] = meta + solana_tx.b58decode(DEX)
    return fake


class JupiterBuildTest(unittest.TestCase):
    def test_pinned_request_is_keyless_and_unwired(self):
        request = jupiter_build.pinned_request()
        self.assertEqual(request["method"], "GET")
        self.assertTrue(request["url"].startswith(jupiter_build.ENDPOINT + "?"))
        self.assertIn("amount=10000000", request["url"])
        self.assertIn(phantom_transport.REQUIRED_WALLET, request["url"])
        self.assertIsNone(request["api_key"])
        self.assertEqual(request["network"], "not_called")
        self.assertNotIn("x-api-key", request["headers"])
        paths = {getattr(route, "path", "") for route in api.router.routes}
        self.assertFalse(any("jupiter" in path for path in paths))
        with self.assertRaises(jupiter_build.BuildError) as caught:
            jupiter_build.exchange(None)
        self.assertEqual(caught.exception.category, "network_disabled")

    def test_fixture_assembles_v0_and_keeps_guards_false(self):
        fake = _rpc()
        shown = jupiter_build.exchange(lambda _request: _Response(_body(), rpc=solana_inspect.RpcClient(fake)))
        self.assertEqual(shown["stage"], "chain_checked")
        self.assertTrue(shown["chain_ok"])
        self.assertEqual(shown["debited_lamports"], 10_000_000)
        self.assertEqual(shown["rent_lamports"], RENT)
        self.assertEqual(shown["blockhash"], BLOCK)
        self.assertEqual(shown["last_valid_block_height"], 250_000_000)
        self.assertFalse(shown["last_valid_block_height_is_provider_expiry"])
        self.assertIsNone(shown["provider_expiry"])
        self.assertFalse(shown["freshness_guarantee"])
        self.assertEqual(shown["price_impact_pct"], "0.001")
        self.assertFalse(shown["price_impact_verified"])
        self.assertEqual(shown["blockhash_fetched_at"], {"secs_since_epoch": 1, "nanos_since_epoch": 0})
        self.assertEqual(shown["other_amount_threshold"], str(FLOOR))
        self.assertTrue(shown["min_output_formula_ok"])
        self.assertFalse(shown["min_output_verified"])
        self.assertFalse(shown["dex_accounts_verified"])
        self.assertEqual(shown["fee_lamports_ceil"], 0)
        self.assertEqual(shown["fee_basis"], "instruction_limit")
        self.assertFalse(shown["fee_authorized"])
        self.assertEqual(shown["fee_policy"], "not_approved")
        self.assertTrue(shown["threshold_matches_floor"])
        self.assertFalse(shown["threshold_rounding_documented"])
        self.assertEqual(shown["freshness_basis"], "last_valid_block_height")
        self.assertFalse(shown["slippage_verified"])
        self.assertFalse(shown["spend_verified"])
        self.assertFalse(shown["intent_verified"])
        self.assertFalse(shown["execution_allowed"])
        self.assertFalse(shown["send"])
        self.assertFalse(shown["route_wired"])
        self.assertEqual(shown["production_provider"], "phantom")
        self.assertIn("simulateTransaction", fake.methods)
        self.assertNotIn("transaction", shown)

    def test_lookup_resolution_preserves_jupiter_accounts(self):
        fake = _rpc()
        parsed = jupiter_build.parse_build(_body())
        raw = base64.b64decode(jupiter_build.assemble(parsed))
        decoded = solana_tx.decode_transaction(raw)
        self.assertEqual(decoded.version, "v0")
        keys = solana_inspect._resolve(decoded, solana_inspect.RpcClient(fake), "confirmed")
        swap = next(item for item in decoded.instructions if item.program_id == solana_jupiter.PROGRAM_ID or keys[item.program_index] == solana_jupiter.PROGRAM_ID)
        self.assertEqual(keys[swap.account_indexes[2]], WALLET)
        self.assertEqual(keys[swap.account_indexes[7]], NATIVE)
        self.assertEqual(keys[swap.account_indexes[8]], USDC)
        self.assertEqual(keys[swap.account_indexes[9]], DEX)
        self.assertGreater(swap.account_indexes[9], len(decoded.static_keys) - 1)

    def test_rejects_redirect_unknown_router_amount_and_slippage(self):
        with self.assertRaises(jupiter_build.BuildError) as redirected:
            jupiter_build.exchange(lambda _request: _Response(_body(), status=302, url="https://example.invalid/build"))
        self.assertEqual(redirected.exception.category, "redirect")
        bad = _body()
        bad["swapInstruction"] = dict(bad["swapInstruction"], programId="proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u")
        with self.assertRaises(jupiter_build.BuildError) as unknown:
            jupiter_build.parse_build(bad)
        self.assertEqual(unknown.exception.category, "unknown_router")
        mismatch = _body(inAmount="10000001")
        with self.assertRaises(jupiter_build.BuildError) as amount:
            jupiter_build.parse_build(mismatch)
        self.assertEqual(amount.exception.category, "amount_mismatch")
        slipped = _body(otherAmountThreshold=str(FLOOR - 1))
        with self.assertRaises(jupiter_build.BuildError) as slippage:
            jupiter_build.parse_build(slipped)
        self.assertEqual(slippage.exception.category, "slippage_mismatch")
        extra = _body(expiresAt=1)
        with self.assertRaises(jupiter_build.BuildError) as malformed:
            jupiter_build.parse_build(extra)
        self.assertEqual(malformed.exception.category, "malformed")
        with self.assertRaises(jupiter_build.BuildError) as version:
            jupiter_build.parse_build(_body(transactionVersion=1))
        self.assertEqual(version.exception.category, "transaction_version_unsupported")
        ceiling = _body(otherAmountThreshold=str(FLOOR + 1))
        self.assertEqual(jupiter_build.parse_build(ceiling)["other_amount_threshold"], str(FLOOR + 1))

    def test_route_v2_goonfi_is_decoded_and_its_accounts_stay_unverified(self):
        self.assertEqual(solana_jupiter.ROUTE_V2.hex(), "bb64facc31c4af14")
        self.assertEqual(solana_jupiter.IDL_SHA256, "e6c2b425ec740922ee91aa7a3278cea07f4bc00a3ed9efa4eca16facb481bfca")
        body = _body(outAmount="897985", otherAmountThreshold="889006")
        event = solana_tx.b58encode(b"\x70" * 32)
        accounts = [
            _account(WALLET, signer=True, writable=True),
            _account(solana_tx.b58encode(b"\x66" * 32), writable=True),
            _account(solana_tx.b58encode(b"\x69" * 32), writable=True),
            _account(NATIVE),
            _account(USDC),
            _account(TOKEN),
            _account(TOKEN),
            _account(solana_tx.b58encode(b"\x69" * 32), writable=True),
            _account(event),
            _account(solana_jupiter.PROGRAM_ID),
            _account(DEX, writable=True),
        ]
        data = (
            solana_jupiter.ROUTE_V2
            + (10_000_000).to_bytes(8, "little")
            + (897985).to_bytes(8, "little")
            + (100).to_bytes(2, "little")
            + (0).to_bytes(2, "little")
            + (0).to_bytes(2, "little")
            + (1).to_bytes(4, "little")
            + bytes([121, 0])
            + (10_000).to_bytes(2, "little")
            + bytes([0, 1])
        )
        self.assertEqual(len(data), 40)
        body["swapInstruction"] = _ix(solana_jupiter.PROGRAM_ID, accounts, data)
        fake = _rpc()
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "dex_accounts_unverified")
        self.assertEqual(shown["instruction_tag"], 121)
        self.assertEqual(shown["simulation_status"], "not_run")
        self.assertNotIn("simulateTransaction", fake.methods)
        self.assertFalse(shown["dex_accounts_verified"])
        self.assertFalse(shown["freshness_guarantee"])
        self.assertFalse(shown["fee_authorized"])
        self.assertTrue(shown["threshold_matches_ceil"])
        self.assertFalse(shown["min_output_verified"])
        self.assertFalse(shown["execution_allowed"])
        priced = _body(outAmount="897985", otherAmountThreshold="889006")
        priced["swapInstruction"] = body["swapInstruction"]
        priced["computeBudgetInstructions"] = [_ix(
            solana_tx.COMPUTE_BUDGET, [], bytes([3]) + (1000).to_bytes(8, "little"),
        )]
        parsed = jupiter_build.parse_build(priced)
        self.assertEqual(parsed["fee_lamports_ceil"], 1400)
        self.assertEqual(parsed["fee_policy"], "not_approved")
        self.assertFalse(parsed["fee_authorized"])
        unknown = bytes(data)
        unknown = unknown[:34] + bytes([120]) + unknown[35:]
        body["swapInstruction"] = _ix(solana_jupiter.PROGRAM_ID, accounts, unknown)
        other = _rpc()
        rejected = jupiter_build.evaluate(body, solana_inspect.RpcClient(other))
        self.assertEqual(rejected["error_category"], "unsupported_route")
        self.assertNotIn("simulateTransaction", other.methods)
        missing_time = _body()
        missing_time["blockhashWithMetadata"] = {"blockhash": list(b"\x11" * 32), "lastValidBlockHeight": 250_000_000}
        with self.assertRaises(jupiter_build.BuildError) as timed:
            jupiter_build.parse_build(missing_time)
        self.assertEqual(timed.exception.category, "malformed")

    def test_whirlpool_pool_vaults_and_oracle_reject_substitution(self):
        import json
        import subprocess
        pool = solana_tx.b58encode(b"\x21" * 32)
        node = subprocess.check_output(["node", "--input-type=module", "-e", """
import { createRequire } from 'module';
const require = createRequire('/tmp/phantom-pin/node_modules/@solana/web3.js/package.json');
const { PublicKey } = require('@solana/web3.js');
const pool = new PublicKey('%s');
const program = new PublicKey('%s');
const [oracle] = PublicKey.findProgramAddressSync([Buffer.from('oracle'), pool.toBuffer()], program);
console.log(JSON.stringify({oracle: oracle.toBase58()}));
""" % (pool, solana_whirlpool.PROGRAM_ID)], text=True)
        self.assertEqual(solana_whirlpool.oracle_address(pool), json.loads(node)["oracle"])
        source = solana_tx.b58encode(b"\x66" * 32)
        dest = solana_tx.b58encode(b"\x69" * 32)
        vault_a = solana_tx.b58encode(b"\x31" * 32)
        vault_b = solana_tx.b58encode(b"\x32" * 32)
        oracle = solana_whirlpool.oracle_address(pool)
        pool_data = solana_whirlpool.reference_pool(NATIVE, vault_a, USDC, vault_b)
        ticks = [solana_tx.b58encode(bytes([40 + i]) * 32) for i in range(3)]
        authority = solana_tx.b58encode(b"\x41" * 32)
        event = solana_tx.b58encode(b"\x70" * 32)
        data = (
            solana_jupiter.ROUTE_V2
            + (10_000_000).to_bytes(8, "little")
            + (897985).to_bytes(8, "little")
            + (100).to_bytes(2, "little")
            + (0).to_bytes(4, "little")
            + (1).to_bytes(4, "little")
            + bytes([17, 1])
            + (10_000).to_bytes(2, "little")
            + bytes([0, 1])
        )
        self.assertEqual(len(data), 40)
        accounts = [
            _account(WALLET, signer=True, writable=True),
            _account(source, writable=True),
            _account(dest, writable=True),
            _account(NATIVE),
            _account(USDC),
            _account(TOKEN),
            _account(TOKEN),
            _account(dest, writable=True),
            _account(event),
            _account(solana_jupiter.PROGRAM_ID),
            _account(TOKEN),
            _account(authority),
            _account(pool, writable=True),
            _account(source, writable=True),
            _account(vault_a, writable=True),
            _account(dest, writable=True),
            _account(vault_b, writable=True),
            _account(ticks[0], writable=True),
            _account(ticks[1], writable=True),
            _account(ticks[2], writable=True),
            _account(oracle),
        ]
        body = _body(outAmount="897985", otherAmountThreshold="889006")
        body["swapInstruction"] = _ix(solana_jupiter.PROGRAM_ID, accounts, data)
        body["addressesByLookupTableAddress"] = {TABLE: [pool]}
        fake = _rpc()
        meta = (1).to_bytes(4, "little") + (b"\xff" * 8) + (b"\x00" * 44)
        fake.tables[TABLE] = meta + solana_tx.b58decode(pool)
        fake.tables[pool] = pool_data
        fake.owners[pool] = solana_whirlpool.PROGRAM_ID
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "whirlpool_authority_unbound")
        self.assertEqual(shown["instruction_tag"], 17)
        self.assertNotIn("simulateTransaction", fake.methods)
        self.assertNotIn("isBlockhashValid", fake.methods)
        self.assertFalse(shown["dex_accounts_verified"])
        self.assertFalse(shown["execution_allowed"])
        self.assertFalse(shown["fee_authorized"])
        bad = list(accounts)
        bad[14] = _account(solana_tx.b58encode(b"\x39" * 32), writable=True)
        body["swapInstruction"] = _ix(solana_jupiter.PROGRAM_ID, bad, data)
        swapped = FakeRpc()
        swapped.tables[TABLE] = fake.tables[TABLE]
        swapped.tables[pool] = pool_data
        swapped.owners[pool] = solana_whirlpool.PROGRAM_ID
        rejected = jupiter_build.evaluate(body, solana_inspect.RpcClient(swapped))
        self.assertEqual(rejected["error_category"], "whirlpool_authority_unbound")
        self.assertNotIn("simulateTransaction", swapped.methods)

    def test_nonzero_fee_does_not_reach_simulation(self):
        fake = _rpc()
        body = _body()
        body["computeBudgetInstructions"][1]["data"] = base64.b64encode(bytes([3]) + (5).to_bytes(8, "little")).decode("ascii")
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "fee_unbound")
        self.assertEqual(shown["simulation_status"], "not_run")
        self.assertNotIn("simulateTransaction", fake.methods)
        self.assertFalse(shown["execution_allowed"])

    def test_third_token_account_rent_is_outside_the_bound(self):
        fake = _rpc()
        body = _body()
        create = body["setupInstructions"][1]
        body["setupInstructions"] = [body["setupInstructions"][0], create, create, create]
        shown = jupiter_build.evaluate(body, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["error_category"], "rent_unbound")
        self.assertNotIn("simulateTransaction", fake.methods)


class RentSplitTest(unittest.TestCase):
    def test_rent_is_not_added_to_the_swap_cap(self):
        self.assertEqual(RENT, 1_488_440)
        system = solana_tx.SYSTEM_PROGRAM
        create = (
            (0).to_bytes(4, "little")
            + RENT.to_bytes(8, "little")
            + (165).to_bytes(8, "little")
            + solana_tx.b58decode(TOKEN)
        )
        transfer = (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")
        tx = solana_tx.encode_legacy(WALLET, [WALLET, NEW, system], 1, [
            (2, [0, 1], create),
            (2, [0, 0], transfer),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["debited_lamports"], 10_000_000)
        self.assertEqual(shown["rent_lamports"], RENT)
        self.assertTrue(shown["chain_ok"])
        self.assertFalse(shown["spend_verified"])
