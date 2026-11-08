"""route_v2 minimum, message binding, and the combined fee. Nothing is sent."""
import hashlib
import json
import time
import unittest
from decimal import Decimal
from pathlib import Path

import usdc_valuation

import jupiter_route_v2
import phantom_transport
import solana_jupiter
import solana_tx
import solana_whirlpool

WALLET = phantom_transport.REQUIRED_WALLET
TOKEN = solana_tx.b58encode(b"\x55" * 32)
SQRT = 5531210526019943285
CAPTURED = bytes.fromhex("bb64facc31c4af14809698000000000061910d000000000032000000000001000000c310270001")
def _quote():
    return {
        "feed_id": usdc_valuation.FEED_ID,
        "price": 100_000_000,
        "conf": 0,
        "expo": -8,
        "publish_time": int(time.time()),
        "origin": "test_harness",
        "authenticated_live": False,
    }


def _route(quoted: int, slippage: int, a_to_b: bool = True, amount: int = 1_000_000) -> bytes:
    step = bytes([17, 1 if a_to_b else 0]) + (10_000).to_bytes(2, "little") + bytes([0, 1])
    return (
        jupiter_route_v2.DISC
        + amount.to_bytes(8, "little")
        + quoted.to_bytes(8, "little")
        + slippage.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (1).to_bytes(4, "little")
        + step
    )


def _pool(source_mint: str, dest_mint: str, fee_rate: int = 100, liquidity: int = 10**18):
    pool = solana_tx.b58encode(b"\x21" * 32)
    wsol = solana_tx.associated_token_address(WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    source = wsol if source_mint == solana_tx.NATIVE_MINT else solana_tx.b58encode(b"\x66" * 32)
    dest = wsol if dest_mint == solana_tx.NATIVE_MINT else solana_tx.b58encode(b"\x69" * 32)
    vault_a = solana_tx.b58encode(b"\x31" * 32)
    vault_b = solana_tx.b58encode(b"\x32" * 32)
    spacing, current = 8, 16
    base = solana_whirlpool.start_tick_index(current, spacing)
    step = solana_whirlpool.TICK_ARRAY_SIZE * spacing
    starts = [base - i * step for i in range(3)]
    ticks = [solana_whirlpool.tick_array_address(pool, start) for start in starts]
    values = {
        "pool": pool, "source": source, "dest": dest, "vault_a": vault_a, "vault_b": vault_b,
        "ticks": ticks, "oracle": solana_whirlpool.oracle_address(pool),
        "authority": WALLET, "token_program": solana_tx.TOKEN_PROGRAM,
    }
    pool_data = solana_whirlpool.reference_pool(
        source_mint, vault_a, dest_mint, vault_b, spacing, current,
        sqrt_price=1 << 64, fee_rate=fee_rate, liquidity=liquidity,
    )
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


def _metas(source_mint: str, dest_mint: str, values: dict) -> list[dict]:
    def meta(pubkey: str, writable: bool = False, signer: bool = False) -> dict:
        return {"pubkey": pubkey, "isWritable": writable, "isSigner": signer}
    hop = [
        meta(solana_whirlpool.PROGRAM_ID),
        meta(values["token_program"]),
        meta(values["authority"]),
        meta(values["pool"], True),
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
        meta(solana_jupiter.PROGRAM_ID),
        *hop,
    ]


def _canonical() -> bytes:
    return solana_whirlpool.reference_pool(
        solana_tx.NATIVE_MINT, solana_tx.b58encode(b"\x11" * 32),
        solana_tx.USDC_MINT, solana_tx.b58encode(b"\x12" * 32),
        sqrt_price=SQRT,
    )


def _tx(metas: list[dict], swap: bytes, limit: int = 200_000, price: int = 1_000, omit_limit: bool = False, side: str = "buy", extra_transfer: str | None = None, signers: int = 1) -> str:
    ordered = []
    def add(pubkey: str) -> None:
        if pubkey not in ordered:
            ordered.append(pubkey)
    add(WALLET)
    writable = []
    readonly = []
    for item in metas:
        if item["pubkey"] == WALLET:
            continue
        bucket = writable if item["isWritable"] else readonly
        if item["pubkey"] not in writable and item["pubkey"] not in readonly and item["pubkey"] not in ordered:
            bucket.append(item["pubkey"])
    wsol = solana_tx.associated_token_address(WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    for pubkey in (wsol, solana_tx.SYSTEM_PROGRAM, solana_tx.TOKEN_PROGRAM, solana_tx.ASSOCIATED_TOKEN, solana_tx.COMPUTE_BUDGET):
        if pubkey not in writable and pubkey not in readonly and pubkey not in ordered:
            (writable if pubkey == wsol else readonly).append(pubkey)
    ordered = [WALLET] + writable + readonly
    index = {pubkey: pos for pos, pubkey in enumerate(ordered)}
    amount = int.from_bytes(swap[8:16], "little")
    create = (index[solana_tx.ASSOCIATED_TOKEN], [index[WALLET], index[wsol], index[WALLET], index[solana_tx.NATIVE_MINT], index[solana_tx.SYSTEM_PROGRAM], index[solana_tx.TOKEN_PROGRAM]], bytes([1]))
    transfer = (index[solana_tx.SYSTEM_PROGRAM], [index[WALLET], index[wsol]], (2).to_bytes(4, "little") + amount.to_bytes(8, "little"))
    sync = (index[solana_tx.TOKEN_PROGRAM], [index[wsol]], bytes([17]))
    close = (index[solana_tx.TOKEN_PROGRAM], [index[wsol], index[WALLET], index[WALLET]], bytes([9]))
    swap_ix = (index[solana_jupiter.PROGRAM_ID], [index[item["pubkey"]] for item in metas], swap)
    budget = index[solana_tx.COMPUTE_BUDGET]
    limit_ix = (budget, [], bytes([2]) + limit.to_bytes(4, "little"))
    price_ix = (budget, [], bytes([3]) + price.to_bytes(8, "little"))
    head = [price_ix] if omit_limit else [limit_ix, price_ix]
    if side == "buy":
        instructions = head + [create, transfer, sync, swap_ix, close]
    else:
        instructions = head + [swap_ix, close]
    if extra_transfer is not None:
        if extra_transfer not in index:
            raise AssertionError("extra")
        instructions.append((index[solana_tx.SYSTEM_PROGRAM], [index[WALLET], index[extra_transfer]], (2).to_bytes(4, "little") + (1).to_bytes(8, "little")))
    return solana_tx.encode_legacy(WALLET, ordered, len(readonly), instructions, solana_tx.b58encode(b"\x44" * 32), required=signers)


class RouteV2Tests(unittest.TestCase):
    def test_idl_bytes_match_the_program_account_hash(self):
        raw = Path("tests/fixtures/jupiter_program_idl.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), solana_jupiter.IDL_SHA256)
        route = next(item for item in json.loads(raw)["instructions"] if item["name"] == "route_v2")
        self.assertEqual(bytes(route["discriminator"]), jupiter_route_v2.DISC)
        self.assertEqual([arg["name"] for arg in route["args"]], [
            "in_amount", "quoted_out_amount", "slippage_bps", "platform_fee_bps", "positive_slippage_bps", "route_plan",
        ])

    def test_lamport_scale_is_one_billion_and_a_token_fee_is_not_lamports(self):
        micro = jupiter_route_v2.lamports_to_micro_usdc(5001, 89_908_537)
        self.assertEqual(micro, 450)
        self.assertGreater(5001 * 89_908_537 // 1_000_000, 400_000)
        as_lamports = jupiter_route_v2.lamports_to_micro_usdc(10_000_000, 89_908_537)
        as_token = jupiter_route_v2.token_raw_to_lamports(10_000_000, 1 << 60, False)
        self.assertNotEqual(as_token, 10_000_000)
        self.assertNotEqual(jupiter_route_v2.lamports_to_micro_usdc(as_token, 89_908_537), as_lamports)

    def test_captured_bytes_do_not_prove_the_programs_rounding(self):
        parsed = jupiter_route_v2.parse(CAPTURED)
        self.assertTrue(parsed["min_out_proven"], parsed)
        self.assertFalse(parsed["program_rounding_proven"])
        self.assertEqual(parsed["min_out"], 884739)
        self.assertEqual(parsed["api_sample_floor"], 884740)
        self.assertEqual(parsed["reason"], "hop_accounts_unpublished")
        self.assertFalse(parsed["send"])

    def test_buy_and_exit_are_bound_to_the_message_and_the_pool_quote(self):
        values, data = _pool(solana_tx.NATIVE_MINT, TOKEN)
        canonical = _canonical()
        buy = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 0)),
            data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertFalse(buy["ok"], buy)
        self.assertEqual(buy["reason"], "usd_not_live")
        self.assertTrue(buy["message_bound"])
        self.assertEqual(buy["amm_est_out"], 999_899)
        self.assertGreaterEqual(buy["min_out"], buy["amm_est_out"])
        self.assertFalse(buy["usd_live"])
        self.assertFalse(buy["send"])
        self.assertFalse(buy["live_ready"])
        self.assertFalse(buy["buy_proven"])
        values_out, data_out = _pool(TOKEN, solana_tx.NATIVE_MINT)
        sell = jupiter_route_v2.decode_message(
            _tx(_metas(TOKEN, solana_tx.NATIVE_MINT, values_out), _route(999_899, 0, True), side="exit"),
            data_out, canonical, _quote(), side="exit", mint=TOKEN,
        )
        self.assertEqual(sell["reason"], "usd_not_live", sell)
        self.assertEqual(sell["dex_fee_unit"], "token_raw")
        self.assertNotEqual(sell["dex_fee_lamports"], jupiter_route_v2.lamports_to_micro_usdc(sell["dex_fee_raw"], 89_908_537))

    def test_a_caller_blob_a_one_raw_quote_and_an_unknown_account_fail(self):
        values, data = _pool(solana_tx.NATIVE_MINT, TOKEN)
        canonical = _canonical()
        loose = jupiter_route_v2.decode(
            _metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 50),
            side="buy", mint=TOKEN, account_data=data, compute=[], canonical_pool=canonical, quote=_quote(),
        )
        self.assertEqual(loose["reason"], "message_unbound")
        self.assertFalse(loose["ok"])
        tiny = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 9999)),
            data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(tiny["reason"], "min_below_amm")
        self.assertFalse(tiny["ok"])
        cheap = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 0)),
            data, canonical, {"usable": True, "upper": "0.01", "origin": "test_harness"}, side="buy", mint=TOKEN,
        )
        self.assertEqual(cheap["reason"], "valuation_fields")
        self.assertFalse(cheap["ok"])
        huge = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 0, amount=200_000_000)),
            data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(huge["reason"], "exposure_cap", huge)
        preexisting = dict(data)
        raw = bytearray(preexisting[values["source"]][1])
        raw[64:72] = (10**12).to_bytes(8, "little")
        preexisting[values["source"]] = (preexisting[values["source"]][0], bytes(raw))
        held = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 0)),
            preexisting, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(held["reason"], "preexisting_wsol")
        self.assertFalse(held.get("wrap_verified"))
        metas = _metas(solana_tx.NATIVE_MINT, TOKEN, values)
        metas.insert(7, {"pubkey": solana_tx.b58encode(b"\x44" * 32), "isWritable": False, "isSigner": False})
        unknown = jupiter_route_v2.decode_message(
            _tx(metas, _route(999_899, 0)), data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(unknown["reason"], "unidentified_account")
        missing = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 50), omit_limit=True),
            data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(missing["reason"], "compute_unit_limit_missing")
        rich, rich_data = _pool(solana_tx.NATIVE_MINT, TOKEN, fee_rate=60_000)
        capped = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, rich), _route(1_000_000, 50, amount=50_000_000)),
            rich_data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(capped["reason"], "fee_cap", capped)
        self.assertFalse(capped["live_ready"])
        two = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 0), signers=2),
            data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(two["reason"], "extra_signer")
        self.assertEqual(two["required_signatures"], 2)
        corrupt = dict(data)
        tick = values["ticks"][0]
        blob = bytearray(corrupt[tick][1])
        blob[0] ^= 0xFF
        corrupt[tick] = (corrupt[tick][0], bytes(blob))
        bad = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 0)),
            corrupt, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertFalse(bad["ok"])
        self.assertNotEqual(bad.get("amm_est_out"), 999_899)
        thin, thin_data = _pool(solana_tx.NATIVE_MINT, TOKEN, liquidity=10**9)
        plain = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, thin), _route(998_901, 0)),
            thin_data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        crossed = dict(thin_data)
        tick = thin["ticks"][0]
        blob = bytearray(crossed[tick][1])
        slot = (8 - 0) // 8
        at = 12 + slot * 113
        blob[at] = 1
        blob[at + 1:at + 17] = (-(10**9 // 2)).to_bytes(16, "little", signed=True)
        crossed[tick] = (crossed[tick][0], bytes(blob))
        moved = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, thin), _route(998_901, 0)),
            crossed, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(plain.get("amm_est_out"), 998_901)
        self.assertEqual(moved.get("amm_est_out"), 1_000_297)
        self.assertNotEqual(moved.get("amm_est_out"), plain.get("amm_est_out"))
        readonly = _metas(solana_tx.NATIVE_MINT, TOKEN, values)
        tick_meta = next(item for item in readonly if item["pubkey"] == values["ticks"][0])
        tick_meta["isWritable"] = False
        denied = jupiter_route_v2.decode_message(
            _tx(readonly, _route(999_899, 0)), data, canonical, _quote(), side="buy", mint=TOKEN,
        )
        self.assertEqual(denied["reason"], "privilege")
