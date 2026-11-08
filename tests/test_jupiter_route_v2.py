"""route_v2 minimum, Whirlpool accounts, and the combined fee. Nothing is sent."""
import unittest
from decimal import Decimal

import jupiter_route_v2
import phantom_transport
import solana_tx
import solana_whirlpool

WALLET = phantom_transport.REQUIRED_WALLET
TOKEN = solana_tx.b58encode(b"\x55" * 32)
# Captured from a keyless SOL/USDC /build. ZeroFi hop, so the accounts stay unverified.
CAPTURED = bytes.fromhex("bb64facc31c4af14809698000000000061910d000000000032000000000001000000c310270001")


def _route(quoted: int, slippage: int, a_to_b: bool = True) -> bytes:
    step = bytes([17, 1 if a_to_b else 0]) + (10_000).to_bytes(2, "little") + bytes([0, 1])
    return (
        jupiter_route_v2.DISC
        + (1_000_000).to_bytes(8, "little")
        + quoted.to_bytes(8, "little")
        + slippage.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (1).to_bytes(4, "little")
        + step
    )


def _accounts(source_mint: str, dest_mint: str, values: dict) -> list[dict]:
    def meta(pubkey: str, writable: bool = False, signer: bool = False) -> dict:
        return {"pubkey": pubkey, "isWritable": writable, "isSigner": signer}
    hop = [
        meta(solana_whirlpool.PROGRAM_ID),
        meta(values["token_program"]),
        meta(values["authority"]),
        meta(values["pool"]),
        meta(values["source"], True),
        meta(values["vault_a"], True),
        meta(values["dest"], True),
        meta(values["vault_b"], True),
        *[meta(tick, True) for tick in values["ticks"]],
        meta(values["oracle"]),
    ]
    return [
        meta(WALLET, signer=True),
        meta(values["source"], True),
        meta(values["dest"], True),
        meta(source_mint),
        meta(dest_mint),
        meta(solana_tx.TOKEN_PROGRAM),
        meta(solana_tx.TOKEN_PROGRAM),
        meta(jupiter_route_v2.EVENT),
        meta("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"),
        *hop,
    ]


def _pool(source_mint: str, dest_mint: str, fee_rate: int = 100):
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
    values = {
        "pool": pool, "source": source, "dest": dest, "vault_a": vault_a, "vault_b": vault_b,
        "ticks": ticks, "starts": starts, "oracle": solana_whirlpool.oracle_address(pool),
        "authority": WALLET, "token_program": solana_tx.TOKEN_PROGRAM,
    }
    pool_data = solana_whirlpool.reference_pool(source_mint, vault_a, dest_mint, vault_b, spacing, current, fee_rate=fee_rate)
    accounts = {
        pool: (solana_whirlpool.PROGRAM_ID, pool_data),
        source: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(source_mint, WALLET)),
        dest: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(dest_mint, WALLET)),
        vault_a: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(source_mint, solana_tx.b58encode(b"\x91" * 32))),
        vault_b: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(dest_mint, solana_tx.b58encode(b"\x92" * 32))),
    }
    for address, start in zip(ticks, starts):
        accounts[address] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.reference_tick_array(pool, start))
    return values, accounts


class RouteV2Tests(unittest.TestCase):
    def test_captured_bytes_prove_the_floor_and_ignore_a_json_minimum(self):
        parsed = jupiter_route_v2.parse(CAPTURED)
        self.assertTrue(parsed["min_out_proven"], parsed)
        self.assertEqual(parsed["min_out"], 884740)
        self.assertEqual(parsed["quoted_out_amount"], 889185)
        self.assertEqual(parsed["slippage_bps"], 50)
        self.assertNotEqual(parsed["min_out"], 1)
        self.assertEqual(parsed["reason"], "hop_accounts_unpublished")
        self.assertFalse(parsed["send"])
        self.assertIsNone(jupiter_route_v2.minimum_out(100, 10_000))

    def test_buy_and_exit_bind_the_whirlpool_and_the_fee_cap(self):
        values, data = _pool(solana_tx.NATIVE_MINT, TOKEN, fee_rate=100)
        quote = {"usable": True, "upper": Decimal("1"), "lower": Decimal("1")}
        compute = [(2).to_bytes(1, "little") + (200_000).to_bytes(4, "little"), (3).to_bytes(1, "little") + (1_000).to_bytes(8, "little")]
        buy = jupiter_route_v2.decode(
            _accounts(solana_tx.NATIVE_MINT, TOKEN, values), _route(900_000, 50, True),
            side="buy", mint=TOKEN, account_data=data, compute=compute, sol_micro_usdc=1_000_000, quote=quote,
        )
        self.assertTrue(buy["ok"], buy)
        self.assertEqual(buy["min_out"], 900_000 - 900_000 * 50 // 10_000)
        self.assertTrue(buy["dex_fee_proven"])
        self.assertTrue(buy["network_fee_proven"])
        self.assertTrue(buy["combined_fee_usd_proven"])
        self.assertLessEqual(Decimal(buy["combined_fee_usd"]), Decimal("0.05"))
        self.assertTrue(buy["dex_fee_counted_in_input"])
        self.assertFalse(buy["send"])
        self.assertFalse(buy["live_ready"])
        values_out, data_out = _pool(TOKEN, solana_tx.NATIVE_MINT, fee_rate=100)
        sell = jupiter_route_v2.decode(
            _accounts(TOKEN, solana_tx.NATIVE_MINT, values_out), _route(900_000, 50, True),
            side="exit", mint=TOKEN, account_data=data_out, compute=compute, sol_micro_usdc=1_000_000, quote=quote,
        )
        self.assertTrue(sell["ok"], sell)
        self.assertEqual(sell["side"], "exit")
        self.assertFalse(sell["diagnostic_sol_usdc_is_not_this_buy"])

    def test_adversarial_bytes_accounts_and_fees_fail_closed(self):
        values, data = _pool(solana_tx.NATIVE_MINT, TOKEN, fee_rate=100)
        quote = {"usable": True, "upper": Decimal("1"), "lower": Decimal("1")}
        compute = [(2).to_bytes(1, "little") + (200_000).to_bytes(4, "little"), (3).to_bytes(1, "little") + (1_000).to_bytes(8, "little")]
        raw = bytearray(_route(900_000, 50, True))
        raw[16] ^= 0xFF
        changed = jupiter_route_v2.parse(bytes(raw))
        self.assertNotEqual(changed.get("min_out"), 895_500)
        accounts = _accounts(solana_tx.NATIVE_MINT, TOKEN, values)
        accounts[9]["pubkey"] = solana_tx.b58encode(b"\x44" * 32)
        wrong = jupiter_route_v2.decode(accounts, _route(900_000, 50), side="buy", mint=TOKEN, account_data=data, compute=compute, sol_micro_usdc=1_000_000, quote=quote)
        self.assertEqual(wrong["reason"], "whirlpool_program_prefix")
        self.assertFalse(wrong["send"])
        missing = jupiter_route_v2.decode(
            _accounts(solana_tx.NATIVE_MINT, TOKEN, values), _route(900_000, 50),
            side="buy", mint=TOKEN, account_data=data, compute=[compute[1]], sol_micro_usdc=1_000_000, quote=quote,
        )
        self.assertEqual(missing["reason"], "compute_unit_limit_missing")
        self.assertFalse(missing["combined_fee_usd_proven"])
        rich, rich_data = _pool(solana_tx.NATIVE_MINT, TOKEN, fee_rate=60_000)
        capped = jupiter_route_v2.decode(
            _accounts(solana_tx.NATIVE_MINT, TOKEN, rich), _route(900_000, 50),
            side="buy", mint=TOKEN, account_data=rich_data, compute=compute, sol_micro_usdc=2_000_000, quote=quote,
        )
        self.assertEqual(capped["reason"], "fee_cap")
        self.assertFalse(capped["ok"])
        diagnostic = jupiter_route_v2.decode(
            _accounts(solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, values), _route(900_000, 50),
            side="buy", mint=solana_tx.USDC_MINT, account_data=data, compute=compute, sol_micro_usdc=1_000_000, quote=quote,
        )
        self.assertEqual(diagnostic["reason"], "diagnostic_is_not_a_token_buy")
